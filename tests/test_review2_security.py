"""Second security review: a free ``launch`` command is admin, the data folder does not adopt what it did
not write, the API on IPv6 loopback too, the setup page's token, connection caps, the installers, the
build, signature renewal, the update download's deadline and the 429 answer."""
from __future__ import annotations

import errno
import io
import json
import os
import re
import socket
import subprocess
import sys
import time

import pytest

import pclink.api as api_mod
from pclink import codesign, hardening
from pclink import updater as up
from pclink.api import ApiServer, AuthThrottle, address_bucket, is_admin_command, listen_plan
from pclink.config import ApiConfig, UpdatesConfig
from pclink.webui import SETUP_PAGE

from .test_api import FakeBackend, WsClient, call

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = sys.platform == "win32"


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


def serve(backend=None, **cfg):
    cfg.setdefault("token", "s3cret")
    srv = ApiServer(ApiConfig(enabled=True, host=cfg.pop("host", "127.0.0.1"), port=0, **cfg), backend or FakeBackend())
    srv.start()
    return srv


def raw(port: int, text: str, host: str = "127.0.0.1", timeout: float = 5.0) -> bytes:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))
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


def ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


# -- 1. launch with a command line of its own needs the admin level ------------------------------------------

def test_a_free_launch_command_is_admin_a_configured_app_is_not():
    assert is_admin_command({"action": "launch", "command": "cmd /c whoami"})
    assert is_admin_command({"action": "LAUNCH", "name": "minecraft", "command": "x"})
    assert is_admin_command({"action": "launch", "command": ["x"]})            # anything the service might pass on
    assert not is_admin_command({"action": "launch", "name": "minecraft", "user": "kevin"})
    assert not is_admin_command({"action": "launch", "name": "minecraft", "command": ""})
    assert not is_admin_command({"action": "launch", "name": "minecraft", "command": None})
    # the entity path only knows configured apps: launch_<slug> carries no command
    assert not is_admin_command({"action": "set", "entity": "launch_minecraft", "value": "cmd /c whoami"})


def test_the_everyday_token_cannot_launch_a_command_over_http_or_websocket():
    backend = FakeBackend()
    srv = serve(backend, admin_token="b0ss")
    try:
        status, body = call(srv, "POST", "/api/command", {"action": "launch", "command": "calc.exe"}, token="s3cret")
        assert status == 403 and "admin" in body["error"] and not backend.commands
        assert call(srv, "POST", "/api/command", {"action": "launch", "name": "Minecraft"}, token="s3cret")[0] == 200
        assert call(srv, "POST", "/api/command", {"action": "launch", "command": "calc.exe"}, token="b0ss")[0] == 200
        ws = WsClient(srv.port, token="s3cret")
        ws.recv_json()
        ws.send(json.dumps({"type": "command", "id": 1, "action": "launch", "command": "calc.exe"}))
        assert "admin" in ws.recv_json()["error"]
        ws.close()
        assert [c.get("command") for c in backend.commands] == [None, "calc.exe"]
    finally:
        srv.stop()


def test_the_documents_say_so():
    assert "`launch` with a `command`" in read("docs", "API.md")
    assert "`launch` with a command line of its own" in read("docs", "SECURITY.md")


# -- 2. the data folder does not adopt what somebody else put there -------------------------------------------------

@pytest.fixture()
def fake_windows(monkeypatch):
    """hardening's Windows calls replaced: icacls succeeds, owners come from a dict (default: Administrators)."""
    monkeypatch.setattr(hardening.sys, "platform", "win32")
    owners: dict[str, str] = {}
    monkeypatch.setattr(hardening, "_owner_sid", lambda item: owners.get(os.path.basename(item), "S-1-5-32-544"))
    monkeypatch.setattr(hardening, "_trusted_owners", lambda: {"S-1-5-18", "S-1-5-32-544"})
    calls = []
    monkeypatch.setattr(hardening, "_icacls", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    restricted = [False]
    monkeypatch.setattr(hardening, "is_restricted", lambda path: restricted[0])
    return owners, restricted, calls


def test_first_securing_moves_foreign_entries_into_quarantine(tmp_path, fake_windows):
    owners, _restricted, calls = fake_windows
    folder = tmp_path / "PCLink"
    (folder / "logs").mkdir(parents=True)
    (folder / "config.yaml").write_text("commands: {evil: calc}")
    (folder / "pclink.db").write_text("db")
    (folder / "mine.txt").write_text("written by an administrator")
    (folder / "logs" / "planted.log").write_text("x")
    (folder / "theirs").mkdir()
    (folder / "theirs" / "inside.txt").write_text("x")
    owners.update({"config.yaml": "S-1-5-21-1-2-3-1001", "pclink.db": "?", "planted.log": "S-1-5-21-1-2-3-1001",
                   "theirs": "S-1-5-21-1-2-3-1001"})
    assert hardening.secure_data_dir(str(folder)) is True
    quarantine = [p for p in folder.iterdir() if p.name.startswith(hardening.QUARANTINE_PREFIX)]
    assert len(quarantine) == 1
    q = quarantine[0]
    assert (q / "config.yaml").exists() and (q / "pclink.db").exists() and (q / "logs" / "planted.log").exists()
    assert (q / "theirs" / "inside.txt").exists()                   # a foreign folder goes as a whole
    assert not (folder / "config.yaml").exists() and not (folder / "theirs").exists()
    assert (folder / "mine.txt").exists() and (folder / "logs").is_dir()
    # the quarantine is secured like everything else (ownership taken, permissions reset)
    touched = {os.path.normcase(a[0]) for a in calls}
    assert os.path.normcase(str(q / "config.yaml")) in touched


def test_a_folder_restricted_already_keeps_its_entries(tmp_path, fake_windows):
    owners, restricted, _calls = fake_windows
    restricted[0] = True
    folder = tmp_path / "PCLink"
    folder.mkdir()
    (folder / "config.yaml").write_text("x")
    owners["config.yaml"] = "S-1-5-21-1-2-3-1001"      # an administrator's own account, say
    assert hardening.secure_data_dir(str(folder)) is True
    assert (folder / "config.yaml").exists()
    assert not [p for p in folder.iterdir() if p.name.startswith(hardening.QUARANTINE_PREFIX)]


def test_what_cannot_be_moved_aside_is_a_refusal(tmp_path, fake_windows, monkeypatch):
    owners, _restricted, _calls = fake_windows
    folder = tmp_path / "PCLink"
    folder.mkdir()
    (folder / "config.yaml").write_text("x")
    owners["config.yaml"] = "S-1-5-21-1-2-3-1001"

    def busy(src, dst):
        raise PermissionError(errno.EACCES, "in use")

    monkeypatch.setattr(hardening.os, "rename", busy)
    assert hardening.secure_data_dir(str(folder)) is False
    with pytest.raises(hardening.DataDirError):
        hardening.ensure_data_dir(str(folder))


def test_without_pywin32_an_unrestricted_folder_with_content_is_refused(tmp_path, fake_windows, monkeypatch):
    monkeypatch.setattr(hardening, "_owner_sid", lambda item: None)
    folder = tmp_path / "PCLink"
    folder.mkdir()
    assert hardening.secure_data_dir(str(folder)) is True          # empty: nothing to judge
    (folder / "config.yaml").write_text("x")
    assert hardening.secure_data_dir(str(folder)) is False


def test_hard_links_and_quarantines_on_a_first_securing(tmp_path, fake_windows):
    folder = tmp_path / "PCLink"
    folder.mkdir()
    outside = tmp_path / "system-file.txt"
    outside.write_text("not ours")
    try:
        os.link(outside, folder / "pclink.db")
    except OSError:
        pytest.skip("no hard links here")
    (folder / "quarantine-20260101-000000").mkdir()               # an earlier one is left alone
    assert hardening.secure_data_dir(str(folder)) is True
    moved = [p for p in folder.iterdir() if p.name.startswith(hardening.QUARANTINE_PREFIX) and any(p.iterdir())]
    assert len(moved) == 1 and (moved[0] / "pclink.db").exists() and not (folder / "pclink.db").exists()


@pytest.mark.skipif(not WINDOWS, reason="real ACLs")
def test_a_users_temp_folder_is_not_restricted(tmp_path):
    pytest.importorskip("win32security")
    assert hardening.is_restricted(str(tmp_path)) is False


def test_installer_lists_foreign_files_before_anything_touches_the_folder():
    script = read("packaging", "install.ps1")
    listing = script.index("belong to another account")
    assert listing < script.index("& $staged init-config") < script.index("& $staged check-config")
    assert "quarantine-" in script
    assert "quarantine-" in read("docs", "SECURITY.md")


# -- 3. the API's port on IPv6 loopback as well ---------------------------------------------------------------------

def test_listen_plan():
    assert listen_plan("127.0.0.1") == [(socket.AF_INET, "127.0.0.1", True), (socket.AF_INET6, "::1", False)]
    assert listen_plan("localhost") == listen_plan("127.0.0.1")
    assert listen_plan("::1") == [(socket.AF_INET6, "::1", True), (socket.AF_INET, "127.0.0.1", False)]
    assert listen_plan("0.0.0.0") == [(socket.AF_INET, "0.0.0.0", True), (socket.AF_INET6, "::", False)]
    assert listen_plan("192.168.1.20") == [(socket.AF_INET, "192.168.1.20", True)]
    assert listen_plan("fe80::1") == [(socket.AF_INET6, "fe80::1", True)]


@pytest.mark.skipif(not ipv6_loopback(), reason="no IPv6 loopback here")
def test_the_api_takes_its_port_on_ipv6_loopback_too():
    srv = serve()
    try:
        port = srv.port
        reply = raw(port, f"GET /api/health HTTP/1.1\r\nHost: [::1]:{port}\r\nConnection: close\r\n\r\n", host="::1")
        assert reply.startswith(b"HTTP/1.1 200")
        if WINDOWS:
            other = socket.socket(socket.AF_INET6)
            with pytest.raises(OSError):
                other.bind(("::1", port))
            other.close()
    finally:
        srv.stop()
    # and it is let go again (on Linux the closed connection keeps the port in TIME_WAIT for a
    # plain bind; SO_REUSEADDR passes that there, but still not a port somebody listens on)
    s = socket.socket(socket.AF_INET6)
    if not WINDOWS:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("::1", port))
    s.close()


@pytest.mark.skipif(not ipv6_loopback(), reason="no IPv6 loopback here")
def test_somebody_on_the_ipv6_port_stops_the_start():
    squatter = socket.socket(socket.AF_INET6)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    squatter.bind(("::1", 0))
    squatter.listen(1)
    port = squatter.getsockname()[1]
    srv = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=port, token="s3cret"), FakeBackend())
    try:
        with pytest.raises(OSError):
            srv.start()
        assert srv._server is None
        probe = socket.socket()          # the IPv4 side that was bound first was released again
        probe.bind(("127.0.0.1", port))
        probe.close()
    finally:
        squatter.close()


def test_no_ipv6_does_not_stop_the_api(monkeypatch):
    original = api_mod._Server

    class NoIPv6(original):
        def __init__(self, address, handler, tls=None, family=socket.AF_INET, caps=None):
            if family == socket.AF_INET6:
                raise OSError(errno.EADDRNOTAVAIL, "no IPv6")
            super().__init__(address, handler, tls=tls, family=family, caps=caps)

    monkeypatch.setattr(api_mod, "_Server", NoIPv6)
    srv = serve()
    try:
        assert call(srv, "GET", "/api/health")[0] == 200 and srv._extra == []
    finally:
        srv.stop()


# -- 4. the setup page keeps the token for the tab only ------------------------------------------------------------

def test_setup_page_keeps_the_token_in_session_storage_and_can_forget_it():
    assert "sessionStorage" in SETUP_PAGE
    assert not re.search(r"localStorage\.(getItem|setItem)", SETUP_PAGE)
    assert 'localStorage.removeItem("pclink_token")' in SETUP_PAGE      # what an earlier version left is cleared
    assert 'id="forgetTok"' in SETUP_PAGE and '$("forgetTok").onclick' in SETUP_PAGE


# -- 5. connection caps: the PC is one address, a reserve for tokens -------------------------------------------

def test_addresses_of_the_pc_are_one_bucket():
    for a in ("127.0.0.1", "127.3.4.5", "::1", "::ffff:127.0.0.9"):
        assert address_bucket(a) == "loopback"
    assert address_bucket("192.168.1.5") == "192.168.1.5"
    assert address_bucket("2001:db8::1") == address_bucket("2001:db8::ffff:1") == "2001:db8::/64"
    assert address_bucket("2001:db8:0:1::1") != address_bucket("2001:db8::1")
    throttle = AuthThrottle(failures=2, window=60, lockout=60)
    throttle.failed("127.0.0.1")
    assert throttle.failed("127.0.0.2") is True and throttle.blocked("::1") > 0   # rotating 127.x does not help


def test_caps_reserve_and_promotion(monkeypatch):
    monkeypatch.setattr(api_mod, "MAX_CONNECTIONS", 10)
    monkeypatch.setattr(api_mod, "TOKEN_RESERVE", 4)
    monkeypatch.setattr(api_mod, "MAX_CONNECTIONS_PER_ADDRESS", 3)
    monkeypatch.setattr(api_mod, "TOKEN_RESERVE_PER_ADDRESS", 2)
    caps = api_mod._Caps()
    local = [caps.admit(f"127.0.0.{i}") for i in range(1, 4)]
    assert [s.state for s in local] == ["open"] * 3                    # one bucket: the 4th is not an ordinary one
    pending = [caps.admit("127.0.0.9"), caps.admit("::1")]
    assert [s.state for s in pending] == ["pending"] * 2
    assert caps.admit("127.0.0.1") is None                              # its reserve is used up as well
    assert caps.admit("192.168.1.7").state == "open"                    # another address is not affected
    caps.promote(pending[0])
    assert pending[0].state == "token" and caps.pending == 1
    assert caps.admit("127.0.0.1").state == "pending"
    caps.promote(local[0])                                              # a token on an ordinary place frees it
    assert caps.admit("127.0.0.1").state == "open"
    for slot in local + pending:
        caps.leave(slot)
    assert caps.per_address.get("loopback", 0) == 1 and caps.pending == 1


def test_a_flood_without_tokens_does_not_lock_out_a_client_with_one(monkeypatch):
    monkeypatch.setattr(api_mod, "MAX_CONNECTIONS_PER_ADDRESS", 3)
    monkeypatch.setattr(api_mod, "HEAD_TIMEOUT", 1.0)
    srv = serve()
    held = []
    try:
        for _ in range(3):
            held.append(socket.create_connection(("127.0.0.1", srv.port), timeout=5))   # silent: ordinary places
        time.sleep(0.3)
        port = srv.port
        reply = raw(port, "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 503") and b"Retry-After: 5" in reply
        assert call(srv, "GET", "/api/status", token="s3cret")[0] == 200    # Home Assistant still gets in
        # a place in the reserve that sends nothing is given up quickly
        idle = socket.create_connection(("127.0.0.1", port), timeout=5)
        started = time.monotonic()
        assert idle.recv(10) == b"" and time.monotonic() - started < 3.0
        idle.close()
    finally:
        for sock in held:
            sock.close()
        srv.stop()


# -- 6. / 7. installers ------------------------------------------------------------------------------------------

def test_install_service_makes_no_firewall_rule_without_the_program():
    script = read("scripts", "install-service.ps1")
    rules = [line for line in script.splitlines() if "New-NetFirewallRule" in line]
    assert rules and all("-Program" in line for line in rules)
    assert "exit 1" in script.split("New-NetFirewallRule")[0].split("$program = $null")[1]


def test_defender_exclusion_only_for_an_admin_only_python():
    script = read("packaging", "defender-exclusion.ps1")
    guarded = script.split("if (Test-AdminOnly $python)")[1]
    assert "Add-MpPreference -ExclusionProcess $python" in guarded
    assert "Add-MpPreference -ExclusionProcess $python" not in script.split("if (Test-AdminOnly $python)")[0]


@pytest.mark.skipif(not WINDOWS, reason="PowerShell and NTFS ACLs")
def test_defender_exclusion_acl_check(tmp_path):
    script = read("packaging", "defender-exclusion.ps1")
    functions = script[:script.index('Write-Host "Adding Defender')]
    mine = tmp_path / "python.exe"
    mine.write_bytes(b"")
    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")
    check = (functions + "\n'mine=' + (Test-AdminOnly $env:T_MINE); 'system=' + (Test-AdminOnly $env:T_SYSTEM)")
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    done = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", "-"], input=check.encode(),
                          env=dict(os.environ, T_MINE=str(mine), T_SYSTEM=system), capture_output=True, timeout=120)
    out = done.stdout.decode("utf-8", "replace")
    assert "mine=False" in out, out + done.stderr.decode("utf-8", "replace")
    if os.path.isfile(system):
        assert "system=True" in out, out


# -- 8. the signed build does not upgrade pip from PyPI -----------------------------------------------------------------

def test_build_does_not_upgrade_pip():
    workflow = read(".github", "workflows", "build.yml")
    assert "--upgrade pip" not in workflow and "-U pip" not in workflow


# -- 9. a renewed certificate must come from the same issuing CA ---------------------------------------------------

Sig = codesign.Signature
SUBJECT = "CN=Acme Ltd, O=Acme, C=HU"
ACME = Sig("valid", "AA" * 20, SUBJECT, root="D0" * 20, ca="C0" * 20, ca_subject="CN=Public CA 1", ca_key="K1" * 16)


def signed(signer):
    return codesign.PackageSignatures(files={n: signer for n in codesign.SIGNED}, catalog_matches=True, installed=ACME)


def test_renewal_needs_the_same_issuing_ca():
    renewed = Sig("valid", "AB" * 20, SUBJECT, root="D0" * 20, ca="C0" * 20)
    reissued_ca = Sig("valid", "AC" * 20, SUBJECT, root="D0" * 20, ca="C1" * 20, ca_subject="CN=Public CA 1", ca_key="K1" * 16)
    other_ca_same_root = Sig("valid", "BB" * 20, SUBJECT, root="D0" * 20, ca="C2" * 20, ca_subject="CN=Public CA 2", ca_key="K2" * 16)
    same_name_new_key = Sig("valid", "BC" * 20, SUBJECT, root="D0" * 20, ca="C3" * 20, ca_subject="CN=Public CA 1", ca_key="K3" * 16)
    no_ca_known = Sig("valid", "BD" * 20, SUBJECT, root="D0" * 20)
    assert codesign.same_publisher(renewed, ACME) and codesign.same_publisher(reissued_ca, ACME)
    for other in (other_ca_same_root, same_name_new_key, no_ca_known):
        assert not codesign.same_publisher(other, ACME)
        ok, why = codesign.judge(signed(other))
        assert not ok and other.thumbprint in why
    assert codesign.judge(signed(renewed))[0]
    # an explicitly pinned certificate still decides
    assert codesign.judge(signed(other_ca_same_root), trusted=[other_ca_same_root.thumbprint])[0]
    assert not codesign.judge(signed(renewed), trusted=[other_ca_same_root.thumbprint])[0]


def test_the_issuing_ca_is_recorded(tmp_path):
    path = str(tmp_path / codesign.RECORD_FILE)
    assert codesign.save_record(path, ACME, "1.0")
    loaded = codesign.load_record(path)
    assert (loaded.ca, loaded.ca_subject, loaded.ca_key) == (ACME.ca, ACME.ca_subject, ACME.ca_key)
    old_record = Sig("valid", ACME.thumbprint, SUBJECT, root=ACME.root)   # written by an earlier version
    assert codesign.anchor(old_record, ACME) == (ACME, True)
    assert codesign._by_path({"files": [{"path": "x", "status": "Valid", "thumbprint": "aa", "ca": "c0", "ca_subject": "CN=X",
                                         "ca_key": "k"}]})[os.path.normcase("x")].ca == "C0"
    assert "issued by the same intermediate" in read("docs", "SECURITY.md")


# -- 10. the update download has a deadline ------------------------------------------------------------------------

class Drip(io.BytesIO):
    """A server that sends a little now and then; ``read`` would wait for the whole of n."""

    def __init__(self, data, clock):
        super().__init__(data)
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, n=-1):
        self.clock[0] += 29.0           # just under the socket timeout, every time
        return super().read1(min(n, 10))

    def read(self, n=-1):
        raise AssertionError("read(n) waits for all of n: never used for the network")


def test_the_download_gives_up_after_its_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(up.time, "monotonic", lambda: clock[0])
    data = os.urandom(100 * 1024)
    monkeypatch.setattr(up, "_open", lambda url, accept, token=None: Drip(data, clock))
    upd = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "update"), installed="0.3.0", can_install=True)
    asset = {"browser_download_url": "https://github.com/o/r/releases/download/v0.4.0/PCLink.zip", "size": len(data),
             "digest": "sha256:" + "0" * 64}
    with pytest.raises(TimeoutError):
        upd._download(asset, str(tmp_path / "download.zip"))
    assert clock[0] <= up.DOWNLOAD_DEADLINE + 30
    clock[0] = 0.0
    with pytest.raises(TimeoutError):
        upd._fetch_json("https://api.github.com/repos/o/r/releases/latest")
    assert clock[0] <= up.METADATA_DEADLINE + 30


def test_metadata_is_still_read_whole_and_capped(monkeypatch, tmp_path):
    payload = json.dumps({"tag_name": "v1.0", "body": "x" * 300000}).encode()
    monkeypatch.setattr(up, "_open", lambda url, accept, token=None: _Plain(payload))
    upd = up.SelfUpdate(UpdatesConfig(), str(tmp_path / "update"), installed="0.3.0", can_install=True)
    assert upd._fetch_json("https://api.github.com/x")["tag_name"] == "v1.0"
    monkeypatch.setattr(up, "MAX_METADATA", 1000)
    with pytest.raises(ValueError):
        upd._fetch_json("https://api.github.com/x")


class _Plain(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# -- 11. throttled clients are told when to come back ---------------------------------------------------------------

def test_429_carries_retry_after_over_http_and_websocket():
    srv = serve()
    srv.throttle = AuthThrottle(failures=2, window=60, lockout=60)
    try:
        port = srv.port
        for _ in range(2):
            assert call(srv, "GET", "/api/auth", token="guess")[0] == 401
        # (a read that needs no token on the PC itself is answered all the same; /api/auth always needs one)
        reply = raw(port, "GET /api/auth HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer s3cret\r\n"
                          "Connection: close\r\n\r\n").decode()
        head, _, body = reply.partition("\r\n\r\n")
        assert head.startswith("HTTP/1.1 429")
        wait = int(re.search(r"Retry-After: (\d+)", head).group(1))
        assert 1 <= wait <= 60 and f"try again in {wait} s" in json.loads(body)["error"]
        ws = WsClient(port)                  # without a token: a read-only connection from the PC itself
        ws.recv_json()
        ws.send(json.dumps({"type": "command", "id": 1, "action": "x", "token": "s3cret"}))
        answer = ws.recv_json()
        assert answer["ok"] is False and 1 <= answer["retry_after"] <= 60
        ws.close()
    finally:
        srv.stop()
