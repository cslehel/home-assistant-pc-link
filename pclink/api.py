"""Optional local REST + WebSocket API (stdlib only) and the setup web page.

Runs alongside MQTT; off by default.  Bound to ``127.0.0.1`` unless a token is
configured.  Endpoints (all JSON):

    GET  /                      setup / status page (HTML)
    GET  /api/health            liveness
    GET  /api/status            PC payload (same as the MQTT state topic)
    GET  /api/users             every user's payload
    GET  /api/users/<user>      one user
    GET  /api/events?n=50       recent events
    GET  /api/config            config.yaml text          (token)
    PUT  /api/config            validate + write + reload (token, api.allow_config_edit)
    POST /api/command           the same JSON commands as the MQTT ``cmd`` topic (token)
    POST /api/reload            hot-reload config.yaml    (token)
    GET  /api/ws                WebSocket: pushes {"type":"state",...} on change,
                                accepts the same JSON commands as /api/command

Authentication: ``Authorization: Bearer <token>`` or ``?token=``.  Reads from
loopback need no token; every write does.  Without a token configured the API
is read-only.  A non-loopback bind requires a token for *everything*.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .config import ApiConfig

log = logging.getLogger(__name__)

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ApiBackend:
    """What the orchestrator implements for the API (duck-typed; see core.py)."""

    def api_health(self) -> dict[str, Any]: ...
    def api_status(self) -> dict[str, Any]: ...
    def api_users(self) -> dict[str, dict[str, Any]]: ...
    def api_user(self, user: str) -> Optional[dict[str, Any]]: ...
    def api_events(self, n: int, user: Optional[str]) -> list[dict[str, Any]]: ...
    def api_command(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def api_config_text(self) -> str: ...
    def api_config_write(self, text: str) -> dict[str, Any]: ...
    def api_reload(self) -> dict[str, Any]: ...


class _WsClient:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()
        self.alive = True

    def send_text(self, text: str) -> bool:
        return self.send(0x1, text.encode("utf-8"))

    def send(self, opcode: int, payload: bytes) -> bool:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(n)
        elif n < 65536:
            head.append(126)
            head += struct.pack("!H", n)
        else:
            head.append(127)
            head += struct.pack("!Q", n)
        try:
            with self.lock:
                self.sock.sendall(bytes(head) + payload)
            return True
        except OSError:
            self.alive = False
            return False


def _ws_read_frame(stream) -> Optional[tuple[int, bytes]]:
    """Read one (possibly fragmented) client frame from a file-like; None on close/error."""
    def recv_exact(n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    message = b""
    opcode = None
    while True:
        head = recv_exact(2)
        if head is None:
            return None
        fin = bool(head[0] & 0x80)
        op = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            ext = recv_exact(2)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = recv_exact(8)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]
        if length > 4 * 1024 * 1024:
            return None
        mask = recv_exact(4) if masked else b""
        if masked and mask is None:
            return None
        data = recv_exact(length) if length else b""
        if data is None:
            return None
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op in (0x8, 0x9, 0xA):  # control frames are never fragmented
            return op, data
        if opcode is None:
            opcode = op
        message += data
        if fin:
            return opcode or 0x1, message


class ApiServer:
    def __init__(self, cfg: ApiConfig, backend: ApiBackend, html: str = ""):
        self.cfg = cfg
        self.backend = backend
        self.html = html
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._clients: list[_WsClient] = []
        self._clients_lock = threading.Lock()
        self.started_at = time.time()

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else self.cfg.port

    def start(self) -> None:
        api = self

        class Handler(_Handler):
            server_api = api

        self._server = ThreadingHTTPServer((self.cfg.host, self.cfg.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="api", daemon=True)
        self._thread.start()
        log.info("local API listening on http://%s:%s/", self.cfg.host, self.port)

    def stop(self) -> None:
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.send(0x8, b"")
                c.sock.close()
            except OSError:
                pass
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- auth ----------------------------------------------------------------------------------
    def token_ok(self, token: Optional[str]) -> bool:
        if not self.cfg.token:
            return False
        return bool(token) and hmac.compare_digest(str(token), self.cfg.token)

    @property
    def loopback_only(self) -> bool:
        return self.cfg.host in ("127.0.0.1", "localhost", "::1")

    # -- websocket push ------------------------------------------------------------------------
    def push(self, message: dict[str, Any]) -> int:
        with self._clients_lock:
            clients = list(self._clients)
        if not clients:
            return 0
        text = json.dumps(message, default=str)
        sent = 0
        for c in clients:
            if c.send_text(text):
                sent += 1
        with self._clients_lock:
            self._clients = [c for c in self._clients if c.alive]
        return sent

    def _add_client(self, client: _WsClient) -> None:
        with self._clients_lock:
            self._clients.append(client)

    def _remove_client(self, client: _WsClient) -> None:
        with self._clients_lock:
            if client in self._clients:
                self._clients.remove(client)

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)


class _Handler(BaseHTTPRequestHandler):
    server_api: ApiServer  # set by ApiServer.start
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D401 - quiet
        log.debug("api: " + fmt, *args)

    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _token(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        query = parse_qs(urlsplit(self.path).query)
        if "token" in query:
            return query["token"][0]
        return self.headers.get("X-Token")

    def _authorized(self, write: bool) -> bool:
        api = self.server_api
        if api.token_ok(self._token()):
            return True
        if write:
            return False
        return api.loopback_only or self.client_address[0] in ("127.0.0.1", "::1")

    def _body_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}

    def _body_text(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2 * 1024 * 1024:
            raise ValueError("body too large")
        return self.rfile.read(length).decode("utf-8") if length > 0 else ""

    # -- routes -------------------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        api = self.server_api
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/ws":
                self._websocket()
                return
            if not self._authorized(write=False):
                self._json(401, {"error": "unauthorized"})
                return
            if path == "/":
                self._text(200, api.html or "<html><body>pclink</body></html>", "text/html; charset=utf-8")
            elif path == "/api/health":
                self._json(200, api.backend.api_health())
            elif path == "/api/status":
                self._json(200, api.backend.api_status())
            elif path == "/api/users":
                self._json(200, api.backend.api_users())
            elif path.startswith("/api/users/"):
                user = path[len("/api/users/"):]
                data = api.backend.api_user(user)
                self._json(200 if data else 404, data or {"error": "unknown user"})
            elif path == "/api/events":
                query = parse_qs(urlsplit(self.path).query)
                n = int(query.get("n", ["50"])[0])
                user = query.get("user", [None])[0]
                self._json(200, api.backend.api_events(min(max(n, 1), 500), user))
            elif path == "/api/config":
                if not self._authorized(write=True):
                    self._json(401, {"error": "token required"})
                    return
                self._text(200, api.backend.api_config_text(), "text/yaml; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log.exception("api GET %s failed", path)
            try:
                self._json(500, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):  # noqa: N802
        api = self.server_api
        path = urlsplit(self.path).path.rstrip("/")
        try:
            if not self._authorized(write=True):
                self._json(401, {"error": "token required"})
                return
            if path == "/api/command":
                data = self._body_json()
                if not isinstance(data, dict):
                    self._json(400, {"error": "expected a JSON object"})
                    return
                self._json(200, api.backend.api_command(data))
            elif path == "/api/reload":
                self._json(200, api.backend.api_reload())
            else:
                self._json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("api POST %s failed", path)
            self._json(500, {"error": str(exc)})

    def do_PUT(self):  # noqa: N802
        api = self.server_api
        path = urlsplit(self.path).path.rstrip("/")
        try:
            if not self._authorized(write=True):
                self._json(401, {"error": "token required"})
                return
            if path == "/api/config":
                if not api.cfg.allow_config_edit:
                    self._json(403, {"error": "config editing disabled (api.allow_config_edit)"})
                    return
                result = api.backend.api_config_write(self._body_text())
                self._json(200 if result.get("ok") else 400, result)
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log.exception("api PUT %s failed", path)
            self._json(500, {"error": str(exc)})

    # -- websocket -----------------------------------------------------------------------------
    def _websocket(self) -> None:
        api = self.server_api
        if not self._authorized(write=False):
            self._json(401, {"error": "unauthorized"})
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self._json(400, {"error": "websocket upgrade expected"})
            return
        accept = base64.b64encode(hashlib.sha1(key.encode("ascii") + _WS_MAGIC).digest()).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(None)
        client = _WsClient(sock)
        can_write = self._authorized(write=True)
        api._add_client(client)
        try:
            client.send_text(json.dumps({"type": "hello", "writable": can_write, "status": api.backend.api_health()}, default=str))
            while client.alive:
                frame = _ws_read_frame(self.rfile)
                if frame is None:
                    break
                opcode, data = frame
                if opcode == 0x8:
                    client.send(0x8, data[:2])
                    break
                if opcode == 0x9:
                    client.send(0xA, data)
                    continue
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    client.send_text(json.dumps({"type": "error", "error": "invalid json"}))
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type", "command")
                req_id = msg.get("id")
                if kind == "ping":
                    client.send_text(json.dumps({"type": "pong", "id": req_id}))
                elif kind == "status":
                    client.send_text(json.dumps({"type": "status", "id": req_id, "pc": api.backend.api_status(),
                                                 "users": api.backend.api_users()}, default=str))
                elif kind == "command":
                    if not can_write and not api.token_ok(msg.get("token")):
                        client.send_text(json.dumps({"type": "result", "id": req_id, "ok": False, "error": "token required"}))
                        continue
                    payload = {k: v for k, v in msg.items() if k not in ("type", "id", "token")}
                    result = api.backend.api_command(payload)
                    client.send_text(json.dumps({"type": "result", "id": req_id, **result}, default=str))
                else:
                    client.send_text(json.dumps({"type": "error", "id": req_id, "error": f"unknown type {kind}"}))
        except OSError:
            pass
        finally:
            api._remove_client(client)
            try:
                sock.close()
            except OSError:
                pass


def read_config_text(path: Optional[str]) -> str:
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()
