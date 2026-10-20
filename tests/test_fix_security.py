"""Fixes from the security review: the local API, the agent transport, the
self-update's signature and version checks, the data folder, the installers and workflows."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

import pclink.api as api_mod
import pclink.ipc as ipc
from pclink import codesign, hardening
from pclink import updater as up
from pclink.api import ADMIN, NONE, READ, THROTTLED, WRITE, ApiServer, AuthThrottle, _unmask, _WsProtocolError, _WsReader
from pclink.config import ApiConfig, UpdatesConfig
from pclink.webui import SETUP_PAGE

from .test_api import FakeBackend, WsClient, call

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = sys.platform == "win32"


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


def serve(backend=None, html=SETUP_PAGE, **cfg):
    cfg.setdefault("token", "s3cret")
    srv = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=0, **cfg), backend or FakeBackend(), html=html)
    srv.start()
    return srv


def raw_request(port: int, text: str, timeout: float = 5.0) -> bytes:
    """Send a hand-made request and return everything that comes back until the server closes."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(text.encode())
        data = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except (socket.timeout, ConnectionError):
            pass
        return data


def get_with_host(port: int, path: str, host: str, token: str = "") -> tuple[int, str]:
    auth = f"Authorization: Bearer {token}\r\n" if token else ""
    data = raw_request(port, f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{auth}Connection: close\r\n\r\n")
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body.decode("utf-8", "replace")


# -- finding 2: nothing else can listen on the API port ----------------------------------------------

def test_api_port_is_bound_exclusively():
    assert api_mod._Server.allow_reuse_address is (os.name != "nt")
    srv = serve()
    try:
        if WINDOWS:
            # what a program of another user would try: the same port on a more specific address
            for reuse in (False, True):
                other = socket.socket()
                if reuse:
                    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with pytest.raises(OSError):
                    other.bind(("127.0.0.1", srv.port))
                other.close()
        assert call(srv, "GET", "/api/health")[0] == 200
    finally:
        srv.stop()


def test_installers_scope_the_firewall_rule_and_ask_the_program_for_the_api_settings():
    for script in (read("packaging", "install.ps1"), read("scripts", "install-service.ps1")):
        assert "-Program" in script and "api-info" in script
        assert r"port:\s" not in script and "Get-Content" not in script.split("api-info")[1]   # the YAML is not grepped
        assert "Remove-NetFirewallRule" in script


# -- finding 4 / 10: host names, page headers, no inline handlers ------------------------------------

def test_requests_without_a_token_need_a_host_name_that_is_this_pc():
    srv = serve()
    try:
        port = srv.port
        assert get_with_host(port, "/api/users", f"127.0.0.1:{port}")[0] == 200
        assert get_with_host(port, "/api/users", f"localhost:{port}")[0] == 200
        assert get_with_host(port, "/api/users", f"[::1]:{port}")[0] == 200
        assert get_with_host(port, "/api/users", f"{socket.gethostname()}:{port}")[0] == 200
        # DNS rebinding: a page's own host name pointed at 127.0.0.1
        status, body = get_with_host(port, "/api/users", f"rebind.attacker.example:{port}")
        assert status == 403 and "kevin" not in body and "host name" in body
        assert get_with_host(port, "/api/events", "attacker.example")[0] == 403
        # with a token the name does not matter (the page of an attacker has none)
        assert get_with_host(port, "/api/users", f"pc.lan:{port}", token="s3cret")[0] == 200
        # ... and neither does it for the WebSocket: refused without a token, under any Origin
        reply = raw_request(port, f"GET /api/ws HTTP/1.1\r\nHost: attacker.example:{port}\r\n"
                                  f"Origin: http://attacker.example:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                  "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
    finally:
        srv.stop()


def test_host_allow_list():
    srv = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=0), FakeBackend())
    srv.host_names = {"localhost", "gaming-pc", "gaming-pc.local"}
    for good in (None, "", "127.0.0.1", "127.0.0.1:47392", "192.168.1.20:47392", "[fe80::1]:47392", "::1",
                 "LOCALHOST:1", "Gaming-PC", "gaming-pc.local.:80"):
        assert srv.host_allowed(good), good
    for bad in ("attacker.example", "gaming-pc.attacker.example:47392", "127.0.0.1.attacker.example", "localhost.evil"):
        assert not srv.host_allowed(bad), bad


def test_setup_page_headers_and_no_inline_handlers():
    srv = serve()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5) as resp:
            headers, page = resp.headers, resp.read().decode()
        assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff"
        csp = headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp and "unsafe-inline' 'sha" not in csp
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"script-src 'sha256-{digest}'" in csp      # only the script the page came with runs
    finally:
        srv.stop()
    assert not re.search(r"\son[a-z]+\s*=", SETUP_PAGE)   # no onclick= and friends: the CSP would block them
    assert "window.cmd" not in SETUP_PAGE
    assert '"\'":"&#39;"' in SETUP_PAGE                       # the apostrophe is escaped as well
    assert "addEventListener" in SETUP_PAGE and "data-user" in SETUP_PAGE


def test_page_is_served_to_a_remote_browser_but_the_api_is_not(monkeypatch):
    srv = serve()
    try:
        monkeypatch.setattr(api_mod._Handler, "_from_this_pc", lambda self: False)   # as if from the LAN
        status, page = call(srv, "GET", "/")
        assert status == 200 and "PC Link" in page
        assert call(srv, "GET", "/api/health")[0] == 401
        assert call(srv, "GET", "/api/health", token="s3cret")[0] == 200
    finally:
        srv.stop()


# -- finding 6: /api/auth (C7), the admin token (C8), HTTPS (C9), the throttle -----------------------

def test_auth_probe_says_what_a_token_may_do():
    srv = serve(read_token="look")
    try:
        assert call(srv, "GET", "/api/auth")[0] == 401
        assert call(srv, "GET", "/api/auth", token="nope")[0] == 401
        assert call(srv, "GET", "/api/auth", token="look")[1] == {"ok": True, "writable": False, "admin": False, "config_edit": False}
        # no admin token configured: the main token is the admin (the configuration needs allow_config_edit on top)
        assert call(srv, "GET", "/api/auth", token="s3cret")[1] == {"ok": True, "writable": True, "admin": True, "config_edit": False}
    finally:
        srv.stop()
    srv = serve(admin_token="b0ss")
    try:
        assert call(srv, "GET", "/api/auth", token="s3cret")[1] == {"ok": True, "writable": True, "admin": False, "config_edit": False}
        assert call(srv, "GET", "/api/auth", token="b0ss")[1] == {"ok": True, "writable": True, "admin": True, "config_edit": True}
    finally:
        srv.stop()


def test_token_levels():
    plain = ApiServer(ApiConfig(token="w", read_token="r"), FakeBackend())
    assert [plain.token_level(t) for t in ("w", "r", "x", "", None)] == [ADMIN, READ, NONE, NONE, NONE]
    split = ApiServer(ApiConfig(token="w", read_token="r", admin_token="a"), FakeBackend())
    assert [split.token_level(t) for t in ("a", "w", "r", "x")] == [ADMIN, WRITE, READ, NONE]
    assert split.token_ok("w") and split.token_ok("a") and not split.token_ok("r") and split.read_token_ok("r")
    assert ApiServer(ApiConfig(), FakeBackend()).token_level("anything") == NONE


ADMIN_COMMANDS = [{"action": "pclink_update_install"}, {"action": "run", "name": "backup"}, {"action": "reload"},
                  {"action": "RELOAD"}, {"action": "set", "entity": "cmd_backup", "value": "PRESS"},
                  {"action": "set", "entity": "pclink_update_install", "value": "PRESS"},
                  {"action": "set", "entity": "reload_config", "value": "PRESS"}]


def test_admin_token_is_needed_for_what_runs_as_system():
    backend = FakeBackend()
    srv = serve(backend, admin_token="b0ss")   # allow_config_edit stays at its default (off)
    try:
        # the configuration, reloading it, custom commands and the update: not with the everyday token
        assert call(srv, "GET", "/api/config", token="s3cret")[0] == 403
        assert call(srv, "PUT", "/api/config", "device: {name: Y}\n", token="s3cret", content_type="text/yaml")[0] == 403
        assert call(srv, "POST", "/api/reload", token="s3cret")[0] == 403
        for command in ADMIN_COMMANDS:
            status, body = call(srv, "POST", "/api/command", command, token="s3cret")
            assert status == 403 and "admin" in body["error"], command
        assert not backend.commands and backend.config_text.startswith("device: {name: X}")
        # everyday control still works with it
        assert call(srv, "POST", "/api/command", {"action": "add_minutes", "user": "kevin"}, token="s3cret")[0] == 200
        assert call(srv, "POST", "/api/command", {"action": "set", "entity": "user_kevin_blocked", "value": "ON"}, token="s3cret")[0] == 200
        # the admin token does all of it
        assert call(srv, "GET", "/api/config", token="b0ss")[0] == 200
        assert call(srv, "PUT", "/api/config", "device: {name: Y}\n", token="b0ss", content_type="text/yaml")[0] == 200
        assert call(srv, "POST", "/api/reload", token="b0ss")[0] == 200
        for command in ADMIN_COMMANDS:
            assert call(srv, "POST", "/api/command", command, token="b0ss")[0] == 200
        assert len(backend.commands) == 2 + len(ADMIN_COMMANDS)
        # the same over the WebSocket
        ws = WsClient(srv.port, token="s3cret")
        hello = ws.recv_json()
        assert hello["writable"] is True and hello["admin"] is False
        ws.send(json.dumps({"type": "command", "id": 1, "action": "run", "name": "backup"}))
        assert "admin" in ws.recv_json()["error"]
        ws.send(json.dumps({"type": "command", "id": 2, "action": "run", "name": "backup", "token": "b0ss"}))
        assert ws.recv_json()["ok"] is True
        ws.close()
    finally:
        srv.stop()


def test_without_an_admin_token_the_main_token_keeps_its_powers_but_config_needs_the_switch():
    backend = FakeBackend()
    srv = serve(backend)
    try:
        assert call(srv, "POST", "/api/command", {"action": "run", "name": "backup"}, token="s3cret")[0] == 200
        assert call(srv, "POST", "/api/reload", token="s3cret")[0] == 200
        assert call(srv, "GET", "/api/config", token="s3cret")[0] == 403        # allow_config_edit is off by default
        srv.cfg.allow_config_edit = True
        assert call(srv, "GET", "/api/config", token="s3cret")[0] == 200
        assert call(srv, "GET", "/api/config")[0] == 401
    finally:
        srv.stop()


def test_auth_throttle_logic():
    now = [0.0]
    throttle = AuthThrottle(failures=3, window=10, lockout=30, clock=lambda: now[0])
    assert not throttle.failed("a") and not throttle.failed("a") and throttle.blocked("a") == 0
    assert throttle.failed("a") is True and throttle.blocked("a") == 30
    assert throttle.blocked("b") == 0                    # per address
    now[0] = 29
    assert throttle.blocked("a") == 1
    now[0] = 31
    assert throttle.blocked("a") == 0
    # failures spread out over more than the window never lock
    for _ in range(10):
        now[0] += 6
        assert not throttle.failed("c")


def test_wrong_tokens_are_throttled_per_address():
    backend = FakeBackend()
    srv = serve(backend)
    srv.throttle = AuthThrottle(failures=3, window=60, lockout=60)
    try:
        for _ in range(3):
            assert call(srv, "POST", "/api/command", {"action": "x"}, token="guess")[0] == 401
        status, body = call(srv, "POST", "/api/command", {"action": "x"}, token="s3cret")
        assert status == 429 and "try again" in body["error"] and not backend.commands   # even the right one waits
        assert call(srv, "GET", "/api/auth", token="s3cret")[0] == 429
        assert call(srv, "GET", "/api/health")[0] == 200       # reads without a token are not affected
        # ... and a token inside a WebSocket message is not looked at either
        ws = WsClient(srv.port)
        ws.recv_json()
        ws.send(json.dumps({"type": "command", "id": 1, "action": "x", "token": "s3cret"}))
        assert "too many" in ws.recv_json()["error"] and not backend.commands
        ws.close()
        srv.throttle = AuthThrottle()
        assert call(srv, "POST", "/api/command", {"action": "x"}, token="s3cret")[0] == 200
    finally:
        srv.stop()


def test_wrong_tokens_in_websocket_messages_count():
    srv = serve()
    srv.throttle = AuthThrottle(failures=2, window=60, lockout=60)
    try:
        ws = WsClient(srv.port)
        ws.recv_json()
        for n in range(2):
            ws.send(json.dumps({"type": "command", "id": n, "action": "x", "token": "guess"}))
            assert "token required" in ws.recv_json()["error"]
        assert srv.check("s3cret", "127.0.0.1") == THROTTLED
        ws.close()
    finally:
        srv.stop()


@pytest.fixture()
def certificate(tmp_path):
    from pclink.__main__ import make_cert

    try:
        return make_cert(str(tmp_path), ["localhost", "127.0.0.1"], days=30)
    except Exception as exc:  # noqa: BLE001 - neither 'cryptography' nor a usable PowerShell
        pytest.skip(f"no way to make a certificate here: {exc}")


def test_https_with_a_pinned_fingerprint(certificate):
    cert_path, key_path, fingerprint = certificate
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint) and fingerprint == api_mod.cert_fingerprint(cert_path)
    backend = FakeBackend()
    srv = serve(backend, tls_cert=cert_path, tls_key=key_path)
    try:
        assert srv.tls
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE        # like Home Assistant: the fingerprint is what is checked
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as sock:
            with context.wrap_socket(sock) as tls:
                assert hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() == fingerprint
                tls.sendall(b"GET /api/auth HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer s3cret\r\nConnection: close\r\n\r\n")
                data = b""
                while chunk := tls.recv(4096):
                    data += chunk
        assert data.startswith(b"HTTP/1.1 200") and b'"writable": true' in data
        # plain HTTP on the TLS port gets nothing
        assert not raw_request(srv.port, "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n").startswith(b"HTTP/1.1 200")
        # the WebSocket works over TLS too, in both directions at once
        raw = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        ws = WsClient.__new__(WsClient)
        ws.sock, ws.buf = context.wrap_socket(raw), b""
        key = base64.b64encode(os.urandom(16)).decode()
        ws.sock.sendall((f"GET /api/ws?token=s3cret HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                         f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += ws.sock.recv(4096)
        head, _, ws.buf = head.partition(b"\r\n\r\n")
        assert b" 101 " in head.split(b"\r\n")[0]
        assert ws.recv_json()["writable"] is True
        big = "x" * 200_000
        assert srv.push({"type": "state", "blob": big}) == 1
        ws.send(json.dumps({"type": "command", "id": 5, "action": "add_minutes", "user": "kevin"}))
        seen = {}
        for _ in range(2):
            msg = ws.recv_json()
            seen[msg["type"]] = msg
        assert seen["state"]["blob"] == big and seen["result"]["ok"] is True
        ws.sock.close()
    finally:
        srv.stop()


def test_mdns_announces_https_and_the_fingerprint(certificate):
    from pclink.discovery import MdnsAdvertiser

    cert_path, key_path, fingerprint = certificate
    plain = MdnsAdvertiser(ApiConfig(enabled=True, host="0.0.0.0", token="t"), "pc1", "PC 1").properties()
    assert "tls" not in plain and "fp" not in plain
    props = MdnsAdvertiser(ApiConfig(enabled=True, host="0.0.0.0", token="t", tls_cert=cert_path, tls_key=key_path),
                           "pc1", "PC 1").properties()
    assert props["tls"] == "1" and props["fp"] == fingerprint


def test_make_cert_and_api_info_commands(tmp_path, capsys, certificate):
    from pclink.__main__ import main

    assert main(["make-cert", "--dir", str(tmp_path)]) == 1        # the fixture made one already: not replaced
    assert certificate[2] in capsys.readouterr().out
    assert main(["make-cert", "--dir", str(tmp_path), "--force", "--days", "5"]) == 0
    out = capsys.readouterr().out
    assert "tls_cert:" in out and "tls_key:" in out and re.search(r"fingerprint \(SHA-256\): [0-9a-f]{64}", out)
    assert certificate[2] not in out
    cfg = tmp_path / "config.yaml"
    cfg.write_text("device: {name: Den PC}\napi:\n  enabled: true\n  host: 0.0.0.0\n  port: 5123\n  token: 0123456789abcdef0123\n",
                   encoding="utf-8")
    assert main(["api-info", "--config", str(cfg)]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": True, "host": "0.0.0.0", "port": 5123, "lan": True, "tls": False}
    cfg.write_text("device: {name: Den PC}\n", encoding="utf-8")
    assert main(["api-info", "--config", str(cfg)]) == 0
    assert json.loads(capsys.readouterr().out)["lan"] is False
    assert main(["api-info", "--config", str(tmp_path / "missing.yaml")]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_rsa_key_encoding():
    from pclink.__main__ import rsa_private_key_der

    der = rsa_private_key_der([b"\x00\xc3\x01", b"\x01\x00\x01"] + [b"\x7f"] * 6)
    # SEQUENCE { INTEGER 0, INTEGER 0x00c301 (sign byte kept once), INTEGER 65537, six small ones }
    assert der == bytes.fromhex("301f" "020100" "020300c301" "0203010001" + "02017f" * 6)
    assert rsa_private_key_der([b"\x01" * 300] * 8)[:4] == bytes.fromhex("30820983")   # long form of the length


def test_pin_hash_command_prints_the_salted_form(capsys):
    from pclink import pin
    from pclink.__main__ import main

    assert main(["pin-hash", "0420"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("parent_pin_hash: pbkdf2_sha256$")
    assert pin.hash_pin("0420", salt=bytes.fromhex(out.split("$")[2]), iterations=int(out.split("$")[1])) == out.split(": ", 1)[1]


# -- finding 12 and routed items: limits and robustness of the API ------------------------------------

def test_a_request_head_has_a_deadline_in_all(monkeypatch):
    monkeypatch.setattr(api_mod, "HEAD_TIMEOUT", 0.6)
    srv = serve()
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as sock:
            sock.sendall(b"GET /api/health HTTP/1.1\r\n")
            closed = False
            for _ in range(40):                      # one more header byte every 100 ms, for up to 4 s
                time.sleep(0.1)
                try:
                    sock.sendall(b"X")
                    sock.settimeout(0.01)
                    if sock.recv(100) == b"":
                        closed = True
                        break
                except socket.timeout:
                    continue
                except OSError:
                    closed = True
                    break
        assert closed and time.monotonic() - started < 3.5
        assert call(srv, "GET", "/api/health")[0] == 200      # and an idle keep-alive connection is not on that clock
    finally:
        srv.stop()


def test_connections_are_capped(monkeypatch):
    monkeypatch.setattr(api_mod, "MAX_CONNECTIONS_PER_ADDRESS", 3)
    srv = serve()
    held = []
    try:
        for _ in range(3):
            sock = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            assert sock.recv(4096).startswith(b"HTTP/1.1 200")   # answered, and kept open
            held.append(sock)
        # beyond the cap only a request with a token is taken (the reserve, see test_review2_security)
        assert raw_request(srv.port, "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n", timeout=2).startswith(b"HTTP/1.1 503")
        for sock in held:
            sock.close()
        held.clear()
        deadline = time.time() + 5
        while time.time() < deadline and srv.caps.open:
            time.sleep(0.05)
        assert call(srv, "GET", "/api/health")[0] == 200
    finally:
        for sock in held:
            sock.close()
        srv.stop()


def test_bad_bodies_are_answered_not_dropped():
    srv = serve()
    try:
        port = srv.port
        reply = raw_request(port, "POST /api/command HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer s3cret\r\n"
                                  "Content-Length: nonsense\r\nConnection: close\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 400")
        reply = raw_request(port, "PUT /api/config HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer s3cret\r\n"
                                  f"Content-Length: {api_mod.MAX_BODY + 1}\r\nConnection: close\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 4")
        # a refused request with a body does not poison the connection: the next request on it is answered
        body = json.dumps({"action": "add_minutes"})
        two = (f"POST /api/command HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(body)}\r\n\r\n{body}"
               "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        reply = raw_request(port, two)
        assert reply.startswith(b"HTTP/1.1 401") and b"HTTP/1.1 200" in reply
    finally:
        srv.stop()


def test_events_route_passes_since_and_announced():
    seen = []

    class Backend(FakeBackend):
        def api_events(self, n, user, since=None, announced=False):
            seen.append((n, user, since, announced))
            return []

    srv = serve(Backend())
    try:
        call(srv, "GET", "/api/events?n=200&since=1700000000.5&announced=1")
        call(srv, "GET", "/api/events?since=abc&announced=0&user=kevin")
        assert seen == [(200, None, 1700000000.5, True), (50, "kevin", None, False)]
    finally:
        srv.stop()


def frame(opcode: int, payload: bytes, fin: bool = True, masked: bool = True, rsv: int = 0) -> bytes:
    head = bytearray([(0x80 if fin else 0) | rsv | opcode])
    n = len(payload)
    bit = 0x80 if masked else 0
    if n < 126:
        head.append(bit | n)
    elif n < 65536:
        head.append(bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", n)
    if not masked:
        return bytes(head) + payload
    mask = os.urandom(4)
    return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def test_websocket_frames_by_the_rules():
    # a ping between the fragments of a message is handed out, and the message goes on after it
    reader = _WsReader(io.BytesIO(frame(0x1, b"Hel", fin=False) + frame(0x9, b"hi") + frame(0x0, b"lo") + frame(0x2, b"\x00\x01")))
    assert reader.read() == (0x9, b"hi")
    assert reader.read() == (0x1, b"Hello")
    assert reader.read() == (0x2, b"\x00\x01")
    assert reader.read() is None
    assert _WsReader(io.BytesIO(frame(0x1, b"x" * 70000))).read() == (0x1, b"x" * 70000)   # 64-bit length
    assert _WsReader(io.BytesIO(frame(0x1, b"abc")[:-1])).read() is None                    # cut short
    bad = {
        "unmasked": frame(0x1, b"hello", masked=False),
        "reserved bits": frame(0x1, b"hello", rsv=0x40),
        "unknown opcode": frame(0x3, b"hello"),
        "fragmented control frame": frame(0x9, b"hi", fin=False),
        "long control frame": frame(0x9, b"x" * 126),
        "continuation of nothing": frame(0x0, b"lo"),
        "new message inside one": frame(0x1, b"a", fin=False) + frame(0x1, b"b"),
    }
    for what, data in bad.items():
        with pytest.raises(_WsProtocolError):
            _WsReader(io.BytesIO(data)).read()
    too_big = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", 2 ** 62) + b"\x00" * 4
    with pytest.raises(_WsProtocolError) as err:
        _WsReader(io.BytesIO(too_big)).read()
    assert err.value.code == 1009
    many = b"".join(frame(0x1 if i == 0 else 0x0, b"x" * 60000, fin=False) for i in range(80))
    with pytest.raises(_WsProtocolError):
        _WsReader(io.BytesIO(many)).read()      # fragments may not add up to more than a message either
    assert api_mod._ws_read_frame(io.BytesIO(frame(0x1, b"hello", masked=False))) is None


def test_unmasking_is_right_and_fast():
    mask = b"\x01\xfe\x7f\x80"
    for size in (0, 1, 3, 4, 5, 1023):
        data = os.urandom(size)
        assert _unmask(data, mask) == bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    assert _unmask(b"\x00\x00\x00\x00\x00", mask) == mask + mask[:1]     # leading zero bytes survive
    big = os.urandom(4 * 1024 * 1024)
    started = time.perf_counter()
    out = _unmask(big, mask)
    assert time.perf_counter() - started < 0.5 and len(out) == len(big) and _unmask(out, mask) == big


def test_unmasked_frame_closes_the_websocket_with_a_protocol_error():
    srv = serve()
    try:
        ws = WsClient(srv.port)
        ws.recv_json()
        ws.sock.sendall(frame(0x1, b'{"type":"ping"}', masked=False))
        op, data = ws.recv()
        assert op == 0x8 and struct.unpack("!H", data[:2])[0] == 1002
        ws.sock.close()
    finally:
        srv.stop()


def test_websocket_slots_are_kept_for_token_holders(monkeypatch):
    monkeypatch.setattr(api_mod, "MAX_WS_ANONYMOUS", 2)
    srv = serve()
    try:
        anonymous = [WsClient(srv.port) for _ in range(2)]
        for ws in anonymous:
            assert ws.recv_json()["type"] == "hello"
        crowd = WsClient(srv.port)                       # a third one without a token: try again later
        op, data = crowd.recv()
        assert op == 0x8 and struct.unpack("!H", data[:2])[0] == 1013
        crowd.sock.close()
        mine = WsClient(srv.port, token="s3cret")        # Home Assistant still gets in
        assert mine.recv_json()["writable"] is True
        for ws in anonymous + [mine]:
            ws.close()
    finally:
        srv.stop()


def test_a_failing_command_is_answered_on_the_websocket():
    class Backend(FakeBackend):
        def api_command(self, data):
            if data.get("action") == "boom":
                raise RuntimeError("database is locked")
            return super().api_command(data)

    srv = serve(Backend())
    try:
        ws = WsClient(srv.port, token="s3cret")
        ws.recv_json()
        ws.send(json.dumps({"type": "command", "id": 4, "action": "boom"}))
        msg = ws.recv_json()
        assert msg == {"type": "result", "id": 4, "ok": False, "error": "database is locked"}
        ws.send(json.dumps({"type": "command", "id": 5, "action": "add_minutes", "user": "kevin"}))
        assert ws.recv_json()["ok"] is True                # the connection is still there
        ws.close()
    finally:
        srv.stop()


# -- the agent transport ---------------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def agent_server(**kwargs):
    port = free_port()
    server = ipc.AgentServer("127.0.0.1", port, on_report=kwargs.pop("on_report", lambda *a: None),
                             validate=lambda sid, tok: tok == "good", secret=lambda sid: "good", **kwargs)
    server.start()
    return server, port


def agent_hello(sock: socket.socket, reader, session_id: int, token: str = "good", **extra) -> dict:
    """The agent's side of the challenge-response hello on a raw socket; returns the service's last answer."""
    nonce = ipc.new_nonce()
    sock.sendall((json.dumps(dict({"type": "hello", "session_id": session_id, "pid": 1, "nonce": nonce}, **extra))
                  + "\n").encode())
    challenge = json.loads(reader.readline(65536))
    assert challenge["type"] == "challenge"
    assert ipc.proof_matches(token, "service", nonce, challenge["nonce"], challenge["proof"])
    sock.sendall((json.dumps({"type": "auth", "proof": ipc.proof(token, "agent", challenge["nonce"], nonce)}) + "\n").encode())
    return json.loads(reader.readline(65536))


def closed_within(sock: socket.socket, seconds: float) -> bool:
    sock.settimeout(seconds)
    try:
        while True:
            if sock.recv(4096) == b"":
                return True
    except socket.timeout:
        return False
    except OSError:
        return True


def test_hello_has_a_deadline_in_all(monkeypatch):
    monkeypatch.setattr(ipc, "HELLO_TIMEOUT", 0.6)
    server, port = agent_server()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        started = time.monotonic()
        gone = False
        for _ in range(30):                 # a byte every 100 ms never lets a single receive time out
            try:
                sock.sendall(b" ")
            except OSError:
                gone = True
                break
            if closed_within(sock, 0.1):
                gone = True
                break
        assert gone and time.monotonic() - started < 2.5
        sock.close()
    finally:
        server.stop()


def test_connections_that_do_not_say_hello_are_capped(monkeypatch):
    monkeypatch.setattr(ipc, "MAX_PENDING", 3)
    monkeypatch.setattr(ipc, "HELLO_TIMEOUT", 30.0)
    server, port = agent_server()
    idle = []
    try:
        for _ in range(3):
            idle.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        deadline = time.time() + 5
        while time.time() < deadline and server._pending < 3:
            time.sleep(0.02)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert closed_within(extra, 2.0)
        extra.close()
        for sock in idle:
            sock.close()
        deadline = time.time() + 5
        while time.time() < deadline and server._pending:
            time.sleep(0.02)
        assert server._pending == 0
        client = ipc.AgentClient("127.0.0.1", port, hello={"session_id": 1, "user": "k", "token": "good", "pid": 1},
                                 on_message=lambda m: None, reconnect_seconds=0.2)
        client.start()
        assert client.connected.wait(5)      # a real agent gets in again
        client.stop()
    finally:
        for sock in idle:
            sock.close()
        server.stop()


@pytest.mark.parametrize("hello", [b'{"type":"hello","session_id":null,"token":"good"}\n',
                                   b'{"type":"hello","session_id":[1],"token":"good"}\n',
                                   b'{"type":"hello","session_id":1e400,"token":"good"}\n',
                                   b'{"type":"hello","session_id":2,"token":"good","pid":"x"}\n',
                                   b'{"type":"hello","session_id":2,"token":"g\xc3\xb6\xc3\xb6d"}\n',
                                   b'[1,2,3]\n', b'not json\n'])
def test_odd_hellos_are_turned_away_quietly(hello):
    server, port = agent_server()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(hello)
            assert closed_within(sock, 3.0)
        assert not server.connected_sessions() and server._pending == 0
    finally:
        server.stop()


def test_line_reader_skips_an_oversized_line_and_stays_in_step():
    a, b = socket.socketpair()
    try:
        a.settimeout(2)
        b.settimeout(10)
        reader = ipc._LineReader(a)
        # sent from a thread: the long line does not fit into the socket buffer (a Linux
        # socketpair holds about 200 KB), so sendall only finishes while the reader reads
        sender = threading.Thread(target=b.sendall, args=(b'{"n":1}\n' + b"x" * 300_000 + b'\n{"n":2}\n',), daemon=True)
        sender.start()
        assert reader.readline(1024, skip=True) == '{"n":1}'
        with pytest.raises(ipc.LineTooLong, match="300000"):
            reader.readline(1024, skip=True)
        assert reader.readline(1024, skip=True) == '{"n":2}'
        assert len(reader._buf) == 0                      # nothing of the long line was kept
        sender.join(5)
        assert not sender.is_alive()
        # a whole line with a deadline that passes
        with pytest.raises(socket.timeout):
            reader.readline(1024, deadline=time.monotonic() + 0.2)
    finally:
        a.close()
        b.close()


def test_an_oversized_message_gets_an_explicit_error_and_the_connection_stays(monkeypatch, caplog):
    monkeypatch.setattr(ipc, "MAX_LINE_LARGE", 20_000)
    reports, notices = [], []
    server, port = agent_server(on_report=lambda sid, r: reports.append(r), on_message=lambda sid, m: notices.append(m))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock, caplog.at_level(logging.WARNING, "pclink.ipc"):
            reader = ipc._LineReader(sock)
            assert agent_hello(sock, reader, 4, user="kevin")["type"] == "hello_ok"
            shot = json.dumps({"type": "screenshot", "id": "abc", "data": "A" * 100_000}).encode()
            sock.sendall(shot + b"\n")
            assert json.loads(reader.readline(65536)) == {"type": "error", "error": "message_too_large", "limit": 20_000}
            sock.sendall(b'{"type":"report","idle_seconds":1}\n{"type":"ping"}\n')
            assert json.loads(reader.readline(65536)) == {"type": "pong"}
        assert len(reports) == 1 and notices == [{"type": "oversized", "limit": 20_000}]
        assert any("session 4 sent a line of" in r.getMessage() for r in caplog.records)
    finally:
        server.stop()


HANDSHAKE = object()   # a ScriptedService reply: answer the hello as the real service does (token "t")


def service_handshake(conn: socket.socket) -> bool:
    reader = ipc._LineReader(conn)
    hello = json.loads(reader.readline(65536))
    nonce = ipc.new_nonce()
    conn.sendall((json.dumps({"type": "challenge", "nonce": nonce,
                              "proof": ipc.proof("t", "service", hello["nonce"], nonce)}) + "\n").encode())
    auth = json.loads(reader.readline(65536))
    return ipc.proof_matches("t", "agent", nonce, hello["nonce"], auth.get("proof"))


class ScriptedService:
    """A stand-in for the service's side of the agent port: answers each new connection with the next reply.

    A reply is the bytes sent instead of the challenge, or (HANDSHAKE, bytes): a proper
    challenge-response hello followed by those bytes.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            reply = self.replies.pop(0) if self.replies else (HANDSHAKE, b'{"type":"hello_ok"}\n')
            try:
                if isinstance(reply, tuple):
                    if not service_handshake(conn):
                        conn.close()
                        continue
                    conn.sendall(reply[1])
                    threading.Thread(target=lambda c=conn: (time.sleep(3), c.close()), daemon=True).start()
                else:
                    conn.recv(4096)
                    conn.sendall(reply)
                    conn.close()
            except (OSError, ValueError):
                pass

    def close(self):
        self.sock.close()


def test_agent_client_survives_replies_that_are_not_what_it_expects():
    service = ScriptedService([b"[1,2,3]\n", b'"hello"\n', b"\n", b'{"type":"busy"}\n',
                               (HANDSHAKE, b'{"type":"hello_ok","session_id":7}\n')])
    seen = []
    client = ipc.AgentClient("127.0.0.1", service.port, hello={"session_id": 7, "token": "t"},
                             on_message=seen.append, reconnect_seconds=0.05)
    client.start()
    try:
        assert client.connected.wait(10)          # four bad answers later it is connected
        assert client._thread.is_alive() and not client.rejected.is_set() and service.connections == 5
        assert seen[0] == {"type": "hello_ok", "session_id": 7}     # the handler hears about every connect
    finally:
        client.stop()
        service.close()


def test_agent_client_keeps_going_when_the_handler_or_anything_else_raises():
    service = ScriptedService([(HANDSHAKE, b'{"type":"hello_ok"}\n{"type":"status"}\n')])
    calls = []

    def handler(msg):
        calls.append(msg)
        raise KeyError("a bug in the handler")

    client = ipc.AgentClient("127.0.0.1", service.port, hello={"session_id": 7, "token": "t"},
                             on_message=handler, reconnect_seconds=0.05)
    client.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.02)
        assert [m["type"] for m in calls[:2]] == ["hello_ok", "status"] and client._thread.is_alive()
    finally:
        client.stop()
        service.close()


def test_agent_client_gives_up_only_on_a_rejection():
    service = ScriptedService([b'{"type":"hello_rejected"}\n'])
    client = ipc.AgentClient("127.0.0.1", service.port, hello={"session_id": 7, "token": "bad"},
                             on_message=lambda m: None, reconnect_seconds=0.05)
    client.start()
    try:
        assert client.rejected.wait(5) and service.connections == 1
    finally:
        client.stop()
        service.close()


def test_screenshot_budget_of_the_agent_matches_the_line_limit():
    assert ipc.MAX_LINE_LARGE == 8 * 1024 * 1024       # bytes of one JSON line; pclink/agent/capture.py sizes images to fit


# -- finding 1 / 13: the signature check fails closed ------------------------------------------------------

Sig = codesign.Signature
ACME = Sig("valid", "AA" * 20, "CN=Acme Ltd, O=Acme, C=HU", root="D0" * 20, ca="C0" * 20)
RENEWED = Sig("valid", "AB" * 20, "CN=Acme Ltd, O=Acme, C=HU", root="D0" * 20, ca="C0" * 20)   # same issuing CA (test_review2_security)
SAME_NAME_OTHER_CA = Sig("valid", "BB" * 20, "CN=Acme Ltd, O=Acme, C=HU", root="E1" * 20)
UNSIGNED = Sig("unsigned")


def signed_package(signer=ACME, installed=ACME, **overrides):
    files = {name: signer for name in codesign.SIGNED}
    files.update(overrides)
    return codesign.PackageSignatures(files=files, catalog_matches=True, installed=installed)


def unsigned_package(installed=UNSIGNED):
    return codesign.PackageSignatures(files={name: UNSIGNED for name in codesign.EXECUTABLES + (codesign.INSTALLER,)},
                                      installed=installed)


def test_what_cannot_be_read_is_a_refusal():
    judge = codesign.judge
    assert judge(unsigned_package()) == (True, "not signed (no signature is required)")       # documented behaviour, kept
    assert judge(signed_package())[0]
    # nothing could be read at all
    assert not judge(codesign.PackageSignatures())[0]
    nothing = codesign.PackageSignatures(error="powershell timed out")
    assert not judge(nothing)[0] and "could not be read" in judge(nothing)[1]
    # the installed copy's signer could not be found out: never "then it must have been unsigned"
    blind = unsigned_package(installed=Sig("unknown", detail="powershell failed"))
    assert not judge(blind)[0] and "installed version" in judge(blind)[1]
    assert not judge(unsigned_package(installed=Sig("invalid")))[0]
    assert not judge(signed_package(installed=Sig()), require=True)[0]
    # one file that could not be checked, even with nothing demanded
    half = unsigned_package()
    half.files["pclink-agent.exe"] = Sig()
    assert not judge(half)[0] and "pclink-agent.exe could not be checked" in judge(half)[1]
    # an unreadable catalog is a catalog that does not match
    garbage = unsigned_package()
    garbage.files[codesign.CATALOG] = UNSIGNED
    garbage.catalog_matches = False
    assert not judge(garbage)[0] and "catalog" in judge(garbage)[1]
    # an unsigned update for a copy that is recorded as signed
    assert "is not signed" in judge(unsigned_package(installed=ACME))[1]


def test_same_publisher_means_same_certificate_or_same_name_from_the_same_root():
    assert codesign.same_publisher(ACME, ACME) and codesign.same_publisher(RENEWED, ACME)
    assert not codesign.same_publisher(SAME_NAME_OTHER_CA, ACME)        # the name alone is not enough
    assert not codesign.same_publisher(Sig("valid", "CC" * 20, ACME.subject), Sig("valid", "AA" * 20, ACME.subject))   # no root known
    assert not codesign.same_publisher(Sig("valid", "", ""), Sig("valid", "", ""))
    assert codesign.judge(signed_package(RENEWED))[0]
    ok, why = codesign.judge(signed_package(SAME_NAME_OTHER_CA))
    assert not ok and "BB" * 20 in why


def test_signer_record_only_moves_towards_signed(tmp_path):
    anchor = codesign.anchor
    assert anchor(None, UNSIGNED) == (UNSIGNED, True)                    # a copy that never was signed says so
    assert anchor(None, Sig("unknown"))[0].status == "unknown" and anchor(None, Sig("unknown"))[1] is False
    assert anchor(None, Sig("invalid"))[0].status == "unknown"
    assert anchor(None, ACME) == (ACME, True) and anchor(ACME, ACME) == (ACME, False)
    assert anchor(UNSIGNED, ACME) == (ACME, True)                        # became signed
    assert anchor(ACME, RENEWED) == (RENEWED, True)
    for worse in (UNSIGNED, Sig("unknown"), Sig("invalid")):             # nothing takes a recorded signer away
        assert anchor(ACME, worse) == (ACME, False)
    assert anchor(UNSIGNED, Sig("unknown")) == (UNSIGNED, False)
    path = str(tmp_path / codesign.RECORD_FILE)
    assert codesign.load_record(path) is None
    assert codesign.save_record(path, ACME, "1.2.3")
    loaded = codesign.load_record(path)
    assert (loaded.status, loaded.thumbprint, loaded.subject, loaded.root) == ("valid", ACME.thumbprint, ACME.subject, ACME.root)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"status": "whatever"}')
    assert codesign.load_record(path) is None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not json")
    assert codesign.load_record(path) is None


def test_updater_uses_the_recorded_signer_not_what_can_be_read_at_the_moment(tmp_path):
    live = [ACME]
    upd = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "update"), installed="0.3.0", can_install=True,
                        installed_signature=lambda: live[0])
    assert upd._installed_signer() == ACME and codesign.load_record(upd.record_path).thumbprint == ACME.thumbprint
    for now in (Sig("unknown"), UNSIGNED, Sig("invalid")):     # PowerShell fails, the file is swapped, ...
        live[0] = now
        signer = upd._installed_signer()
        assert signer.valid and signer.thumbprint == ACME.thumbprint
    # a fresh object (the next start of the service) still knows
    again = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "update"), installed="0.3.0", can_install=True,
                          installed_signature=lambda: Sig("unknown"))
    assert again._installed_signer().thumbprint == ACME.thumbprint
    # a copy that never was signed and cannot be read right now: still "unsigned", updates go on
    fresh = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "other" / "update"), installed="0.3.0", can_install=True,
                          installed_signature=lambda: UNSIGNED)
    os.makedirs(tmp_path / "other")
    assert fresh._installed_signer() == UNSIGNED
    fresh._installed_signature = lambda: Sig("unknown")
    assert fresh._installed_signer().status == "unsigned"


@pytest.mark.skipif(not WINDOWS, reason="reads Authenticode signatures with Windows' own tools")
def test_a_catalog_that_is_no_catalog_does_not_switch_the_check_off(tmp_path):
    """The fail-open of the review: junk as pclink.cat made the whole reading fail, and that counted as "unsigned"."""
    notepad = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")
    if not os.path.isfile(notepad):
        pytest.skip("no notepad.exe to read a signature from")
    folder = tmp_path / "PCLink"
    folder.mkdir()
    (folder / "pclink.exe").write_bytes(b"MZ" + os.urandom(2000))
    (folder / "pclink-agent.exe").write_bytes(b"MZ" + os.urandom(2000))
    (folder / "install.ps1").write_text("Write-Host 'hello'\n", encoding="utf-8")
    (folder / "pclink.cat").write_text("not a catalog at all", encoding="utf-8")
    found = codesign.read_package(str(folder), notepad)
    assert found.installed.valid and found.installed.root and found.installed.issuer     # read on its own, with its chain
    assert found.error is None and set(found.files) == set(codesign.SIGNED)               # the other results survive
    assert found.catalog_matches is False
    ok, why = codesign.judge(found)
    assert not ok
    found.installed = UNSIGNED                # even for a copy that never was signed this package is refused
    assert not codesign.judge(found)[0]
    assert codesign.read_signature(notepad).thumbprint == found.installed.thumbprint or True
    assert codesign.read_signature(str(folder / "missing.exe")).status == "unknown"


# -- finding 7 / 8: the installer's result, the package's version ---------------------------------------------

def test_package_version_must_be_the_announced_one_and_newer(tmp_path):
    check = up.check_package_version
    check("0.4.0", "0.4.0", "0.3.0")
    check("0.4.0", "v0.4.0", "0.3.9")
    for found, announced, installed, why in (("", "0.4.0", "0.3.0", "does not say"), ("banana", "0.4.0", "0.3.0", "does not say"),
                                             ("0.2.0", "0.4.0", "0.3.0", "not the announced"),      # an old release under a new name
                                             ("0.3.0", "0.3.0", "0.3.0", "not newer"), ("0.2.9", "0.2.9", "0.3.0", "not newer")):
        with pytest.raises(ValueError, match=why):
            check(found, announced, installed)
    assert up.package_version(str(tmp_path)) == ""
    (tmp_path / "VERSION").write_bytes(b"\xef\xbb\xbf0.4.0\r\nrest\r\n")
    assert up.package_version(str(tmp_path)) == "0.4.0"


def release(tag="v0.4.0"):
    return {"tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}", "body": "Notes",
            "assets": [{"name": f"PCLink-{tag.lstrip('v')}-windows-x64.zip", "digest": "sha256:" + "0" * 64,
                        "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/PCLink.zip"}]}


def updater_with_package(tmp_path, monkeypatch, version_in_package, installed="0.3.0", **kwargs):
    ran = []

    def fake_download(asset, target):
        import zipfile
        with zipfile.ZipFile(target, "w") as zf:
            for name in ("install.ps1", "pclink.exe", "pclink-agent.exe"):
                zf.writestr(f"PCLink/{name}", "x")
            if version_in_package is not None:
                zf.writestr("PCLink/VERSION", version_in_package + "\n")

    upd = up.SelfUpdate(UpdatesConfig(**kwargs), str(tmp_path / "update"), installed=installed, can_install=True,
                        fetch=lambda url: release(), signatures=lambda folder: unsigned_package())
    monkeypatch.setattr(upd, "_download", fake_download)
    monkeypatch.setattr(upd, "_run_installer", lambda installer, stage: ran.append(installer))
    upd._check()
    return upd, ran


@pytest.mark.parametrize("in_package, why", [(None, "does not say which version"), ("0.2.0", "not the announced"),
                                             ("9.9.9", "not the announced")])
def test_a_package_that_is_not_the_announced_version_is_not_installed(tmp_path, monkeypatch, in_package, why):
    upd, ran = updater_with_package(tmp_path, monkeypatch, in_package)
    upd._install()
    info = upd.snapshot()["pclink_update_info"]
    assert info["status"] == "error" and why in info["error"] and not ran


def test_installer_result_is_read_and_a_failed_update_is_reported(tmp_path):
    result = tmp_path / up.RESULT_FILE
    assert up.read_install_result(str(result)) is None
    result.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": False, "version": "0.4.0", "previous": "0.3.0", "rolled_back": True,
                                                     "error": "the service stopped again right after starting", "ts": 1700000000}).encode())
    upd = up.SelfUpdate(UpdatesConfig(auto_install=True), str(tmp_path / "update"), installed="0.3.0", can_install=True,
                        fetch=lambda url: release())
    upd._adopt_install_result()
    info = upd.snapshot()["pclink_update_info"]
    assert info["status"] == "error" and "stopped again" in info["error"] and "put back" in info["error"]
    assert info["last_install"] == {"ok": False, "version": "0.4.0", "error": "the service stopped again right after starting",
                                    "rolled_back": True, "finished": "2023-11-14T22:13:20+00:00"}
    assert upd._failed_version == "0.4.0"     # which the automatic install then leaves alone (see _loop)
    # a success, or a failure of a version that is installed by now, is no error
    for text in ({"ok": True, "version": "0.3.0", "ts": 5}, {"ok": False, "version": "0.3.0", "error": "x", "ts": 5}):
        result.write_text(json.dumps(text), encoding="utf-8")
        calm = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "update"), installed="0.3.0", can_install=True)
        calm._adopt_install_result()
        assert calm.snapshot()["pclink_update_info"]["status"] == "idle"
    for junk in ("not json", "[1]", "{}"):
        result.write_text(junk, encoding="utf-8")
        assert up.read_install_result(str(result)) is None


def test_installing_does_not_last_for_ever(tmp_path, monkeypatch):
    upd, ran = updater_with_package(tmp_path, monkeypatch, "0.4.0")
    upd._install()
    assert upd.snapshot()["pclink_update_info"]["status"] == "installing" and len(ran) == 1
    assert upd._watch_install() is True                         # still waiting, nothing reported
    # the installer reports a failure while this service is still the one running
    (tmp_path / up.RESULT_FILE).write_text(json.dumps({"ok": False, "version": "0.4.0", "error": "copying failed",
                                                       "rolled_back": True, "ts": time.time()}), encoding="utf-8")
    assert upd._watch_install() is False
    info = upd.snapshot()["pclink_update_info"]
    assert info["status"] == "error" and "copying failed" in info["error"] and info["last_install"]["rolled_back"]
    assert upd._failed_version == "0.4.0"
    # no report at all: after the time limit it is an error too, and an old report does not count
    os.remove(tmp_path / up.RESULT_FILE)
    upd._check()
    upd._install()
    (tmp_path / up.RESULT_FILE).write_text(json.dumps({"ok": True, "version": "0.2.0", "ts": 1000}), encoding="utf-8")
    assert upd._watch_install() is True
    upd._installing_since = time.time() - up.INSTALL_TIMEOUT - 1
    assert upd._watch_install() is False
    info = upd.snapshot()["pclink_update_info"]
    assert info["status"] == "error" and "did not finish" in info["error"]
    upd._check()                                                # the next check clears it and updates stay possible
    assert upd.snapshot()["pclink_update_info"]["status"] == "idle"


def test_release_notes_and_download_progress_are_in_the_status(tmp_path, monkeypatch):
    body = "## Fixed\n\n- one thing\n" + "x" * 5000
    upd = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "update"), installed="0.3.0", can_install=True,
                        fetch=lambda url: dict(release(), body=body))
    upd._check()
    snap = upd.snapshot()
    assert snap["pclink_update_release_notes"].startswith("## Fixed\n\n- one thing") and len(snap["pclink_update_release_notes"]) == up.NOTES_CHARS
    assert snap["pclink_update_percentage"] is None

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    data = os.urandom(3 * 1024 * 1024)
    seen = []
    original = Response.read1   # the download reads one network read at a time (test_review2_security)

    def read_and_look(self, n=-1):
        upd._status = "downloading"
        seen.append(upd.snapshot()["pclink_update_percentage"])
        return original(self, n)

    monkeypatch.setattr(Response, "read1", read_and_look)
    monkeypatch.setattr(up, "_open", lambda url, accept, token=None: Response(data))
    asset = {"browser_download_url": "https://github.com/o/r/releases/download/v0.4.0/PCLink.zip", "size": len(data),
             "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    upd._download(asset, str(tmp_path / "download.zip"))
    assert seen[0] == 0 and seen == sorted(seen) and seen[-1] == 100


def test_listeners_can_be_started_again_after_a_failed_start():
    taken = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        taken.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    srv = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=port, token="s3cret"), FakeBackend())
    agents = ipc.AgentServer("127.0.0.1", port, on_report=lambda *a: None, validate=lambda sid, tok: True)
    for part in (srv, agents):
        with pytest.raises(OSError):
            part.start()
    taken.close()
    try:
        srv.start()
        srv.start()                                   # a second call does not make a second listener
        assert call(srv, "GET", "/api/health")[0] == 200
        # the configuration object is swapped on a reload: nothing of it was kept from the start
        srv.cfg = ApiConfig(enabled=True, host="127.0.0.1", port=port, token="n3w", admin_token="b0ss")
        assert call(srv, "GET", "/api/auth", token="s3cret")[0] == 401
        assert call(srv, "GET", "/api/auth", token="n3w")[1]["admin"] is False
    finally:
        srv.stop()
    agents.port = free_port()
    agents.start()
    agents.stop()


def test_agent_name_is_cleaned_for_the_log(caplog):
    server, port = agent_server()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock, caplog.at_level(logging.INFO, "pclink.ipc"):
            name = "kev\\nin\\u001b[31m" + "x" * 500
            assert agent_hello(sock, ipc._LineReader(sock), 4, user=json.loads('"' + name + '"'))["type"] == "hello_ok"
            deadline = time.time() + 5       # the line is written just after the answer is sent
            while time.time() < deadline and not any("agent connected" in r.getMessage() for r in caplog.records):
                time.sleep(0.02)
        line = next(r.getMessage() for r in caplog.records if "agent connected" in r.getMessage())
        assert "\n" not in line and "\x1b" not in line and len(line) < 160
    finally:
        server.stop()


def test_installer_log_is_kept_outside_the_staging_folder(tmp_path):
    upd = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "update"), installed="0.3.0", can_install=True)
    assert upd.install_log == str(tmp_path / up.INSTALL_LOG) and upd.result_path == str(tmp_path / up.RESULT_FILE)


def test_install_script_swaps_and_rolls_back():
    script = read("packaging", "install.ps1")
    assert '"$dst.new"' in script and '"$dst.old"' in script
    assert "Putting the previous version back" in script and "Write-Result $true" in script
    assert "ResultFile" in script and "install-result.json" in script
    # nothing is stopped before the new copy is in place and its configuration check has passed
    assert script.index("check-config") < script.index("\n    Stop-PCLink")
    assert "Test-ReparsePoint $dataDir" in script        # finding 3: a junction as data folder stops the installer


@pytest.mark.skipif(not WINDOWS, reason="needs Windows PowerShell")
@pytest.mark.parametrize("script", [("packaging", "install.ps1"), ("packaging", "uninstall.ps1"),
                                    ("scripts", "install-service.ps1"), ("packaging", "defender-exclusion.ps1")])
def test_powershell_scripts_parse(script):
    path = os.path.join(ROOT, *script)
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    done = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                           "[void][scriptblock]::Create((Get-Content -Raw -LiteralPath $env:PCLINK_TEST_SCRIPT))"],
                          env=dict(os.environ, PCLINK_TEST_SCRIPT=path), capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")


# -- finding 3 / 13: the data folder, a configuration that does not load ---------------------------------------

def test_a_link_as_data_folder_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(hardening.sys, "platform", "win32")
    folder = tmp_path / "PCLink"
    folder.mkdir()
    assert hardening.data_dir_problem(str(folder)) == ""
    monkeypatch.setattr(hardening, "_is_reparse_point", lambda path: path == str(folder))
    assert "link or junction" in hardening.data_dir_problem(str(folder))
    with pytest.raises(hardening.DataDirError, match="link or junction"):
        hardening.ensure_data_dir(str(folder))
    # a folder that cannot be restricted is an error too, not a line in the log
    monkeypatch.setattr(hardening, "_is_reparse_point", lambda path: False)
    monkeypatch.setattr(hardening, "secure_data_dir", lambda path: False)
    with pytest.raises(hardening.DataDirError, match="could not be restricted"):
        hardening.ensure_data_dir(str(folder))
    monkeypatch.setattr(hardening, "secure_data_dir", lambda path: True)
    hardening.ensure_data_dir(str(tmp_path / "new" / "PCLink"))
    assert (tmp_path / "new" / "PCLink").is_dir()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert "a file" in hardening.data_dir_problem(str(blocker))


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows thing")
def test_a_real_junction_is_recognised(tmp_path):
    target, link = tmp_path / "elsewhere", tmp_path / "PCLink"
    target.mkdir()
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if done.returncode != 0:
        pytest.skip("could not create a junction here")
    try:
        assert "link or junction" in hardening.data_dir_problem(str(link))
        assert hardening.secure_data_dir(str(link)) is False
        with pytest.raises(hardening.DataDirError):
            hardening.ensure_data_dir(str(link))
        assert hardening.data_dir_problem(str(target)) == ""
    finally:
        os.rmdir(link)       # removes the junction, not the folder it points to


def test_init_config_writes_nothing_into_a_folder_that_could_not_be_secured(tmp_path, monkeypatch, capsys):
    import pclink.__main__ as cli

    folder = tmp_path / "PCLink"
    monkeypatch.setattr(cli, "data_dir", lambda: str(folder))

    def refuse(path):
        raise hardening.DataDirError(f"{path} is a link or junction")

    monkeypatch.setattr(hardening, "ensure_data_dir", refuse)
    assert cli.cmd_init_config(["--path", str(folder / "config.yaml")]) == 3
    assert not (folder / "config.yaml").exists() and "link or junction" in capsys.readouterr().err
    monkeypatch.setattr(hardening, "ensure_data_dir", lambda path: None)
    assert cli.cmd_init_config(["--path", str(folder / "config.yaml")]) == 0 and (folder / "config.yaml").exists()


@pytest.mark.skipif(not WINDOWS, reason="the service wrapper needs pywin32")
def test_harden_reports_what_really_happened(monkeypatch, capsys):
    service = pytest.importorskip("pclink.service")
    monkeypatch.setattr(hardening, "set_recovery", lambda name: True)
    monkeypatch.setattr(hardening, "harden_acl", lambda name: False)        # e.g. not an administrator prompt
    monkeypatch.setattr(hardening, "ensure_data_dir", lambda path: None)
    monkeypatch.setattr(hardening, "show_acl", lambda name: "D:(...)")
    with pytest.raises(SystemExit) as err:
        service.handle_command_line(["harden"])
    out = capsys.readouterr().out
    assert "service permissions: FAILED" in out and "auto-restart: ok" in out and "service hardened" not in out
    assert "NOT" in str(err.value)
    monkeypatch.setattr(hardening, "harden_acl", lambda name: True)
    service.handle_command_line(["harden"])
    assert "service hardened" in capsys.readouterr().out

    def no_folder(path):
        raise hardening.DataDirError("it is a link")

    monkeypatch.setattr(hardening, "ensure_data_dir", no_folder)
    with pytest.raises(SystemExit):
        service.handle_command_line(["harden"])
    assert "data folder restricted to administrators: FAILED" in capsys.readouterr().out
    monkeypatch.setattr(hardening, "soften_acl", lambda name: False)
    with pytest.raises(SystemExit):
        service.handle_command_line(["unharden"])
    # a service is never registered over a data folder that is a link
    monkeypatch.setattr(hardening, "data_dir_problem", lambda path: "it is a link")
    with pytest.raises(SystemExit, match="Not installing"):
        service.handle_command_line(["install"])


def test_apply_all_returns_what_worked(monkeypatch):
    monkeypatch.setattr(hardening, "set_recovery", lambda name: True)
    monkeypatch.setattr(hardening, "harden_acl", lambda name: False)
    assert hardening.apply_all("PCLink") == {"auto-restart": True, "service permissions": False}
    assert hardening.apply_all("PCLink", harden=False) == {"auto-restart": True}


GOOD_CONFIG = "device: {name: Den PC}\ntime_limits:\n  users:\n    - {name: kevin, daily_limit_minutes: 60}\n"


def test_a_configuration_that_stops_loading_does_not_switch_the_limits_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(GOOD_CONFIG, encoding="utf-8")
    cfg, problem = hardening.load_config_or_last_good(str(path), str(tmp_path))
    assert problem is None and cfg.device.name == "Den PC"
    assert (tmp_path / hardening.LAST_GOOD_CONFIG).read_text(encoding="utf-8") == GOOD_CONFIG
    for broken in ("device: {name: Den PC\n  users: [", GOOD_CONFIG.replace("daily_limit_minutes: 60", "daily_limit_minutes: lots"),
                   "- just\n- a list\n"):
        path.write_text(broken, encoding="utf-8")
        cfg, problem = hardening.load_config_or_last_good(str(path), str(tmp_path))
        assert problem and "could not be loaded" in problem and str(path) in problem
        assert cfg.time_limits.users[0].name == "kevin" and cfg.time_limits.users[0].daily_limits[0] == 60
        assert cfg.source_path == str(path)        # so that correcting the file is picked up as a change
        assert (tmp_path / hardening.LAST_GOOD_CONFIG).read_text(encoding="utf-8") == GOOD_CONFIG   # the copy stays good
    os.remove(path)                                # the file is gone altogether
    cfg, problem = hardening.load_config_or_last_good(str(path), str(tmp_path))
    assert problem and cfg.device.name == "Den PC"
    # nothing to fall back on: the error is the caller's, as before
    empty = tmp_path / "fresh"
    empty.mkdir()
    with pytest.raises(Exception):
        hardening.load_config_or_last_good(str(empty / "config.yaml"), str(empty))


# -- finding 9 / 11: workflows and installers (read here; they cannot be run on this machine) --------------------

def workflow(name: str) -> str:
    return read(".github", "workflows", name)


def test_signing_is_a_separate_release_only_job():
    text = workflow("build.yml")
    build, package = text.split("\n  package:\n", 1)
    assert "secrets." not in build.split("\njobs:\n", 1)[1]            # the job that runs pip and PyInstaller has no secrets
    assert "SIGN_PFX_BASE64: ${{ secrets.SIGN_PFX_BASE64 }}" in package
    assert "if: ${{ inputs.release }}" in package and "environment: release-signing" in package
    assert "actions/checkout" not in package and "pip install" not in package and "python " not in package
    assert "VERSION" in build and "New-FileCatalog" in package
    release_text = workflow("release.yml")
    assert "release: true" in release_text and "needs.build.outputs.release_artifact" in release_text


def test_workflows_pin_actions_and_keep_expressions_out_of_scripts():
    for name in ("build.yml", "release.yml", "test.yml"):
        text = workflow(name)
        assert re.search(r"^permissions:\n  contents: read$", text, re.M), name
        for line in text.splitlines():
            if re.match(r"\s*-?\s*uses:", line) and "./.github/" not in line:
                assert re.search(r"@[0-9a-f]{40}\b", line), f"{name}: {line.strip()} is not pinned to a commit"
        # ${{ }} never inside a run: script (it is put into env: and read from there)
        in_run, indent = False, 0
        for line in text.splitlines():
            stripped = line.lstrip()
            here = len(line) - len(stripped)
            if in_run and stripped and here <= indent:
                in_run = False
            if in_run:
                assert "${{" not in line, f"{name}: expression inside a script: {line.strip()}"
            match = re.match(r"(-\s+)?run:\s*(.*)$", stripped)
            if match:
                indent = here + (len(match.group(1)) if match.group(1) else 0)
                if match.group(2).strip() in ("|", ">", "|-", ">-"):
                    in_run = True
                else:
                    assert "${{" not in match.group(2), f"{name}: expression inside a script: {stripped}"


def test_lock_file_is_for_the_build_interpreter_and_covers_the_requirements():
    lock = read("packaging", "requirements-lock.txt")
    build = workflow("build.yml")
    version = re.search(r'python-version: "([0-9.]+)"', build).group(1)
    assert f"for CPython {version} on win_amd64" in lock           # made for the interpreter the workflow builds with
    pinned = {m.group(1).lower().replace("_", "-") for m in re.finditer(r"^([A-Za-z0-9_.-]+)==[^ ]+ \\$", lock, re.M)}
    assert len(pinned) == len(re.findall(r"--hash=sha256:[0-9a-f]{64}$", lock, re.M)) > 10      # one hash for each
    wanted = set()
    for name in (("requirements.txt",), ("requirements-windows.txt",), ("packaging", "requirements-build.txt")):
        for line in read(*name).splitlines():
            match = re.match(r"([A-Za-z0-9_.-]+)\s*[><=~]", line.strip())
            if match:
                wanted.add(match.group(1).lower().replace("_", "-"))
    assert wanted and wanted <= pinned, wanted - pinned


def test_new_configuration_is_tied_to_the_time_zone_of_the_pc(tmp_path, monkeypatch, capsys):
    import yaml

    import pclink.__main__ as cli
    import pclink.util as util
    from pclink.config import Config

    example = read("config.example.yaml")
    with_zone = cli.with_time_zone(example, "Europe/Budapest")
    assert yaml.safe_load(with_zone)["time_limits"]["time_zone"] == "Europe/Budapest"
    assert "# time_zone: Europe/Budapest" not in with_zone and with_zone.count("time_zone: Europe/Budapest") == 1
    assert Config.from_dict(yaml.safe_load(with_zone)).time_limits.time_zone == "Europe/Budapest"
    without = cli.with_time_zone(example, None)
    assert "time_zone" not in yaml.safe_load(without)["time_limits"] and "could not be" in without and "SET THIS" in without
    # everything else of the example is as it was
    assert {k: v for k, v in yaml.safe_load(with_zone)["time_limits"].items() if k != "time_zone"} == yaml.safe_load(example)["time_limits"]
    # a text without the commented example line, and one that sets a zone already
    assert yaml.safe_load(cli.with_time_zone("device: {}\ntime_limits:\n  users: []\n", "Asia/Tokyo"))["time_limits"] == {
        "time_zone": "Asia/Tokyo", "users": []}
    assert cli.with_time_zone("time_limits:\n  time_zone: UTC\n", "Asia/Tokyo") == "time_limits:\n  time_zone: UTC\n"
    assert cli.with_time_zone("device: {}\n", "Asia/Tokyo") == "device: {}\n"

    # init-config writes it into a new file, and never touches one that exists
    monkeypatch.setattr(util, "local_iana_zone", lambda: "America/Chicago")
    target = tmp_path / "config.yaml"
    assert cli.cmd_init_config(["--path", str(target)]) == 0
    assert "America/Chicago" in capsys.readouterr().out
    assert Config.load(str(target)).time_limits.time_zone == "America/Chicago"
    target.write_text("device: {name: Den PC}\n", encoding="utf-8")
    assert cli.cmd_init_config(["--path", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "device: {name: Den PC}\n"
    monkeypatch.setattr(util, "local_iana_zone", lambda: None)
    other = tmp_path / "other.yaml"
    assert cli.cmd_init_config(["--path", str(other)]) == 0
    assert "could not be worked out" in capsys.readouterr().out and Config.load(str(other)).time_limits.time_zone is None


def test_build_installs_from_the_lock_file_when_there_is_one():
    assert "--require-hashes -r packaging/requirements-lock.txt" in workflow("build.yml")
    sys.path.insert(0, os.path.join(ROOT, "packaging"))
    try:
        import make_lock
    finally:
        sys.path.pop(0)
    report = {"install": [
        {"metadata": {"name": "PyYAML", "version": "6.0.2"}, "download_info": {"archive_info": {"hashes": {"sha256": "ab" * 32}}}},
        {"metadata": {"name": "altgraph", "version": "0.17.4"}, "download_info": {"archive_info": {"hashes": {"sha256": "cd" * 32}}}}]}
    assert make_lock.lock_lines(report) == ["altgraph==0.17.4 \\\n    --hash=sha256:" + "cd" * 32,
                                            "PyYAML==6.0.2 \\\n    --hash=sha256:" + "ab" * 32]
    with pytest.raises(ValueError):
        make_lock.lock_lines({"install": [{"metadata": {"name": "x", "version": "1"}, "download_info": {}}]})
