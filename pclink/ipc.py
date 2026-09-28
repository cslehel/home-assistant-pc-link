"""Localhost TCP transport between the service and the per-session agents.

Newline-delimited JSON.  Every agent first sends a ``hello`` carrying the
one-time token the service generated when it launched that agent, so only the
agent the service started can report for a given session.  Traffic never
leaves 127.0.0.1.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import AgentReport

log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024
MAX_LINE_LARGE = 8 * 1024 * 1024   # screenshots travel as one base64 JSON line


def _send_line(sock: socket.socket, lock: threading.Lock, message: dict[str, Any]) -> bool:
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with lock:
            sock.sendall(data)
        return True
    except OSError:
        return False


@dataclass
class _Conn:
    sock: socket.socket
    session_id: int
    user: str
    pid: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_seen: float = field(default_factory=time.time)


class AgentServer:
    def __init__(self, host: str, port: int, on_report: Callable[[int, AgentReport], None],
                 validate: Callable[[int, str], bool], on_connect: Optional[Callable[[int, str, int], None]] = None,
                 on_disconnect: Optional[Callable[[int], None]] = None, idle_timeout: float = 120.0,
                 on_message: Optional[Callable[[int, dict[str, Any]], None]] = None):
        self.host, self.port = host, port
        self._on_report, self._validate = on_report, validate
        self._on_connect, self._on_disconnect = on_connect, on_disconnect
        self._on_message = on_message
        self.idle_timeout = idle_timeout
        self._conns: dict[int, _Conn] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(16)
        srv.settimeout(1.0)
        self._server = srv
        self._thread = threading.Thread(target=self._accept_loop, name="agent-server", daemon=True)
        self._thread.start()
        log.info("agent IPC listening on %s:%s", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for c in conns:
            try:
                c.sock.close()
            except OSError:
                pass
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    # -- API ------------------------------------------------------------------
    def connected_sessions(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            return {sid: {"user": c.user, "pid": c.pid, "last_seen": c.last_seen} for sid, c in self._conns.items()}

    def is_connected(self, session_id: int) -> bool:
        with self._lock:
            return session_id in self._conns

    def send(self, session_id: int, message: dict[str, Any]) -> bool:
        with self._lock:
            conn = self._conns.get(session_id)
        if conn is None:
            return False
        ok = _send_line(conn.sock, conn.lock, message)
        if not ok:
            self._drop(conn)
        return ok

    def broadcast(self, message: dict[str, Any]) -> int:
        with self._lock:
            conns = list(self._conns.values())
        return sum(1 for c in conns if _send_line(c.sock, c.lock, message))

    # -- internals -------------------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.5)
                continue
            if addr[0] not in ("127.0.0.1", "::1", self.host):
                sock.close()
                continue
            threading.Thread(target=self._serve, args=(sock,), name="agent-conn", daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        conn: Optional[_Conn] = None
        try:
            sock.settimeout(self.idle_timeout)
            reader = sock.makefile("r", encoding="utf-8", errors="replace", newline="\n")
            hello_line = reader.readline(MAX_LINE)
            hello = json.loads(hello_line) if hello_line else {}
            if hello.get("type") != "hello":
                return
            session_id = int(hello.get("session_id", -1))
            token = str(hello.get("token", ""))
            if session_id < 0 or not self._validate(session_id, token):
                log.warning("rejected agent connection for session %s (bad token)", session_id)
                _send_line(sock, threading.Lock(), {"type": "hello_rejected"})
                return
            conn = _Conn(sock=sock, session_id=session_id, user=str(hello.get("user", "")), pid=int(hello.get("pid", 0)))
            with self._lock:
                old = self._conns.get(session_id)
                self._conns[session_id] = conn
            if old is not None:
                try:
                    old.sock.close()
                except OSError:
                    pass
            _send_line(sock, conn.lock, {"type": "hello_ok", "session_id": session_id})
            log.info("agent connected: session %s user %s pid %s", session_id, conn.user, conn.pid)
            if self._on_connect:
                self._on_connect(session_id, conn.user, conn.pid)
            while not self._stop.is_set():
                try:
                    line = reader.readline(MAX_LINE_LARGE)
                except socket.timeout:
                    if not _send_line(sock, conn.lock, {"type": "ping"}):
                        break
                    continue
                if not line:
                    break
                conn.last_seen = time.time()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "report":
                    try:
                        msg.setdefault("session_id", session_id)
                        report = AgentReport.from_dict(msg)
                        report.session_id = session_id  # never trust the agent's session id
                        self._on_report(session_id, report)
                    except Exception:  # noqa: BLE001
                        log.exception("bad report from session %s", session_id)
                elif kind == "ping":
                    _send_line(sock, conn.lock, {"type": "pong"})
                elif kind == "pong":
                    continue
                elif self._on_message is not None:
                    try:
                        self._on_message(session_id, msg)
                    except Exception:  # noqa: BLE001
                        log.exception("bad %s message from session %s", kind, session_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.debug("agent connection ended: %s", exc)
        finally:
            if conn is not None:
                self._drop(conn)
            else:
                try:
                    sock.close()
                except OSError:
                    pass

    def _drop(self, conn: _Conn) -> None:
        with self._lock:
            if self._conns.get(conn.session_id) is conn:
                del self._conns[conn.session_id]
                removed = True
            else:
                removed = False
        try:
            conn.sock.close()
        except OSError:
            pass
        if removed:
            log.info("agent disconnected: session %s", conn.session_id)
            if self._on_disconnect:
                self._on_disconnect(conn.session_id)


class AgentClient:
    """Agent side: keeps a connection to the service, reconnecting forever."""

    def __init__(self, host: str, port: int, hello: dict[str, Any], on_message: Callable[[dict[str, Any]], None],
                 reconnect_seconds: float = 5.0):
        self.host, self.port = host, port
        self.hello = dict(hello, type="hello")
        self._on_message = on_message
        self.reconnect_seconds = reconnect_seconds
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = threading.Event()
        self.rejected = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="agent-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def send(self, message: dict[str, Any]) -> bool:
        sock = self._sock
        if sock is None or not self.connected.is_set():
            return False
        return _send_line(sock, self._send_lock, message)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.settimeout(90)
                self._sock = sock
                if not _send_line(sock, self._send_lock, self.hello):
                    raise OSError("hello failed")
                reader = sock.makefile("r", encoding="utf-8", errors="replace", newline="\n")
                first = reader.readline(MAX_LINE)
                resp = json.loads(first) if first else {}
                if resp.get("type") != "hello_ok":
                    log.error("service rejected this agent (%s); giving up", resp.get("type"))
                    self.rejected.set()
                    return
                self.connected.set()
                log.info("connected to service at %s:%s", self.host, self.port)
                while not self._stop.is_set():
                    try:
                        line = reader.readline(MAX_LINE)
                    except socket.timeout:
                        if not _send_line(sock, self._send_lock, {"type": "ping"}):
                            break
                        continue
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "ping":
                        _send_line(sock, self._send_lock, {"type": "pong"})
                        continue
                    if msg.get("type") == "pong":
                        continue
                    try:
                        self._on_message(msg)
                    except Exception:  # noqa: BLE001
                        log.exception("message handler failed")
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    log.debug("connection to service failed: %s", exc)
            finally:
                self.connected.clear()
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            self._stop.wait(self.reconnect_seconds)
