import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request

import pytest

from pclink.api import ApiServer
from pclink.config import ApiConfig


class FakeBackend:
    def __init__(self):
        self.commands = []
        self.config_text = "device: {name: X}\n"

    def api_health(self):
        return {"ok": True, "version": "test"}

    def api_status(self):
        return {"cpu_percent": 5, "device_name": "X"}

    def api_users(self):
        return {"kevin": {"user": "kevin", "managed": True}}

    def api_user(self, user):
        return self.api_users().get(user.lower())

    def api_events(self, n, user):
        return [{"ts": 1, "user": user, "kind": "test", "detail": str(n)}]

    def api_command(self, data):
        self.commands.append(data)
        return {"ok": data.get("action") != "bad", "error": None if data.get("action") != "bad" else "bad action"}

    def api_entities(self):
        return {"device": {"identifiers": ["pclink_x"], "name": "X"}, "version": "test", "users": {"kevin": {"name": "Kevin", "managed": True}},
                "entities": [{"unique_id": "pclink_x_cpu", "key": "cpu_percent", "platform": "sensor", "value": "cpu_percent", "source": "pc"}]}

    def api_screenshot(self, user):
        return (b"PNGDATA", "png") if user.lower() == "kevin" else None

    def api_config_text(self):
        return self.config_text

    def api_config_write(self, text):
        if "bad" in text:
            return {"ok": False, "error": "invalid configuration"}
        self.config_text = text
        return {"ok": True, "restart_needed": []}

    def api_reload(self):
        return {"ok": True, "restart_needed": ["storage"]}


@pytest.fixture()
def server():
    backend = FakeBackend()
    cfg = ApiConfig(enabled=True, host="127.0.0.1", port=0, token="s3cret", allow_config_edit=True)
    srv = ApiServer(cfg, backend, html="<html><body>setup</body></html>")
    srv.start()
    yield srv, backend
    srv.stop()


def call(srv, method, path, body=None, token=None, content_type="application/json"):
    url = f"http://127.0.0.1:{srv.port}{path}"
    data = None
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            ct = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ct else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def test_reads_are_open_on_loopback_and_writes_need_token(server):
    srv, backend = server
    assert call(srv, "GET", "/api/health")[1]["ok"]
    assert call(srv, "GET", "/api/status")[1]["cpu_percent"] == 5
    assert call(srv, "GET", "/api/users")[1]["kevin"]["managed"]
    assert call(srv, "GET", "/api/users/KEVIN")[1]["user"] == "kevin"
    assert call(srv, "GET", "/api/users/nobody")[0] == 404
    assert call(srv, "GET", "/api/events?n=3&user=kevin")[1][0]["detail"] == "3"
    assert "setup" in call(srv, "GET", "/")[1]
    assert call(srv, "GET", "/api/nope")[0] == 404
    status, body = call(srv, "POST", "/api/command", {"action": "add_minutes"})
    assert status == 401 and not backend.commands
    status, body = call(srv, "POST", "/api/command", {"action": "add_minutes"}, token="wrong")
    assert status == 401
    status, body = call(srv, "POST", "/api/command", {"action": "add_minutes", "user": "kevin"}, token="s3cret")
    assert status == 200 and body["ok"] and backend.commands[-1]["user"] == "kevin"
    status, body = call(srv, "POST", "/api/command", {"action": "bad"}, token="s3cret")
    assert status == 200 and not body["ok"]
    status, body = call(srv, "POST", "/api/command", "not json", token="s3cret")
    assert status == 400
    assert call(srv, "POST", "/api/reload", token="s3cret")[1]["restart_needed"] == ["storage"]


def test_config_endpoints(server):
    srv, backend = server
    assert call(srv, "GET", "/api/config")[0] == 401
    status, text = call(srv, "GET", "/api/config", token="s3cret")
    assert status == 200 and text.startswith("device:")
    status, body = call(srv, "PUT", "/api/config", "device: {name: Y}\n", token="s3cret", content_type="text/yaml")
    assert status == 200 and body["ok"] and backend.config_text.startswith("device: {name: Y}")
    status, body = call(srv, "PUT", "/api/config", "bad: yes\n", token="s3cret", content_type="text/yaml")
    assert status == 400 and "invalid" in body["error"]
    srv.cfg.allow_config_edit = False
    assert call(srv, "PUT", "/api/config", "device: {}\n", token="s3cret", content_type="text/yaml")[0] == 403


def test_query_token_and_read_only_without_configured_token():
    backend = FakeBackend()
    srv = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=0, token=None), backend)
    srv.start()
    try:
        assert call(srv, "GET", "/api/health")[0] == 200
        assert call(srv, "POST", "/api/command", {"action": "x"}, token="anything")[0] == 401
    finally:
        srv.stop()
    srv2 = ApiServer(ApiConfig(enabled=True, host="127.0.0.1", port=0, token="tok"), backend)
    srv2.start()
    try:
        status, body = call(srv2, "POST", f"/api/command?token=tok", {"action": "x"})
        assert status == 200 and body["ok"]
    finally:
        srv2.stop()


# -- websocket --------------------------------------------------------------------------------

class WsClient:
    """Tiny RFC 6455 client: handshake, masked send, buffered receive."""

    def __init__(self, port, token=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = "/api/ws" + (f"?token={token}" if token else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(4096)
        head, _, self.buf = head.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        assert b"101" in status_line, status_line
        expected = base64.b64encode(hashlib.sha1(key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
        assert expected.encode() in head

    def _exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        head = bytearray([0x81])
        if len(payload) < 126:
            head.append(0x80 | len(payload))
        else:
            head.append(0x80 | 126)
            head += struct.pack("!H", len(payload))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + mask + masked)

    def recv(self):
        head = self._exact(2)
        op = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        return op, self._exact(length)

    def recv_json(self):
        return json.loads(self.recv()[1])

    def close(self):
        self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        try:
            op, _ = self.recv()
            assert op == 0x8
        except (ConnectionError, socket.timeout):
            pass
        self.sock.close()


def test_websocket_hello_commands_and_push(server):
    srv, backend = server
    ws = WsClient(srv.port)
    hello = ws.recv_json()
    assert hello["type"] == "hello" and hello["writable"] is False and hello["status"]["ok"]
    ws.send(json.dumps({"type": "status", "id": 1}))
    msg = ws.recv_json()
    assert msg["type"] == "status" and msg["users"]["kevin"]["managed"] and msg["id"] == 1
    # commands need the token when the connection was not authenticated
    ws.send(json.dumps({"type": "command", "id": 2, "action": "add_minutes"}))
    msg = ws.recv_json()
    assert msg["type"] == "result" and not msg["ok"] and "token" in msg["error"]
    ws.send(json.dumps({"type": "command", "id": 3, "action": "add_minutes", "token": "s3cret", "user": "kevin"}))
    msg = ws.recv_json()
    assert msg["ok"] and backend.commands[-1] == {"action": "add_minutes", "user": "kevin"}
    # server push reaches the client
    deadline = time.time() + 5
    while srv.client_count == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert srv.push({"type": "state", "n": 1}) == 1
    assert ws.recv_json() == {"type": "state", "n": 1}
    # ping / pong and clean close
    ws.send(json.dumps({"type": "ping", "id": 9}))
    assert ws.recv_json()["type"] == "pong"
    ws.close()
    deadline = time.time() + 5
    while srv.client_count and time.time() < deadline:
        time.sleep(0.02)
    assert srv.client_count == 0


def test_websocket_with_token_is_writable(server):
    srv, backend = server
    ws = WsClient(srv.port, token="s3cret")
    assert ws.recv_json()["writable"] is True
    ws.send(json.dumps({"action": "focus", "user": "kevin"}))  # type defaults to command
    msg = ws.recv_json()
    assert msg["ok"] and backend.commands[-1]["action"] == "focus"
    ws.close()


def test_entities_catalog_and_screenshot_endpoints(server):
    srv, backend = server
    status, body = call(srv, "GET", "/api/entities")
    assert status == 200 and body["entities"][0]["key"] == "cpu_percent" and body["users"]["kevin"]["managed"]
    # screenshots always need the token, even on loopback
    assert call(srv, "GET", "/api/users/kevin/screenshot")[0] == 401
    url = f"http://127.0.0.1:{srv.port}/api/users/kevin/screenshot"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer s3cret"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200 and resp.headers["Content-Type"] == "image/png" and resp.read() == b"PNGDATA"
    assert call(srv, "GET", "/api/users/dad/screenshot", token="s3cret")[0] == 404
    assert call(srv, "GET", "/api/users/kevin")[1]["user"] == "kevin"  # plain user route still works


def test_websocket_entities_request(server):
    srv, backend = server
    ws = WsClient(srv.port, token="s3cret")
    hello = ws.recv_json()
    assert hello["type"] == "hello" and hello["writable"] is True
    ws.send(json.dumps({"type": "entities", "id": 7}))
    msg = ws.recv_json()
    assert msg["type"] == "entities" and msg["id"] == 7 and msg["entities"][0]["platform"] == "sensor"
    assert msg["device"]["name"] == "X"
    ws.close()
