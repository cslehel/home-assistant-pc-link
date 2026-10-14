"""Localhost TCP transport between the service and the per-session agents.

Newline-delimited JSON.  Traffic never leaves 127.0.0.1.

Both ends prove that they know the one-time token the service generated when
it launched the agent, and neither ever sends the token itself:

1. agent   -> ``hello``     session, user, pid, a fresh nonce ``nonce`` (no token)
2. service -> ``challenge`` its own fresh nonce and ``proof`` = HMAC(token, "service|" + agent nonce + "|" + service nonce)
3. agent   -> ``auth``      ``proof`` = HMAC(token, "agent|" + service nonce + "|" + agent nonce)
4. service -> ``hello_ok``  (or ``hello_rejected``)

So only the agent the service started can report for a given session, and an
agent obeys nothing (``run``, ``close``, ``screenshot`` ...) from a program that
took the port while the service was not holding it: that program cannot answer
the challenge, and what it hears is of no use with the real service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import AgentReport

log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024
MAX_LINE_LARGE = 8 * 1024 * 1024   # screenshots travel as one base64 JSON line
HELLO_TIMEOUT = 10.0               # seconds a new connection gets to identify itself, in all
SOCKET_TIMEOUT = 5.0               # per send / receive call, so a stuck peer cannot stall the caller
MAX_PENDING = 32                   # connections that have not identified themselves yet (each one is a thread)
MAX_SKIP = 64 * 1024 * 1024        # how much of one oversized line is thrown away before the connection is dropped
PROTOCOL = 2                       # 2: challenge-response hello (1 sent the token itself)
MAX_BAD_PROOFS = 3                 # answers in a row that could not prove the token: the agent gives up (it is stale)
_NONCE_RE = re.compile(r"^[0-9a-f]{32,128}$")


def new_nonce() -> str:
    return secrets.token_hex(16)


def good_nonce(value: Any) -> bool:
    return isinstance(value, str) and bool(_NONCE_RE.match(value))


def proof(token: str, role: str, first: str, second: str) -> str:
    """HMAC-SHA256 of ``role|first|second`` with the launch token (pure; unit-tested).

    The role is part of what is signed, so one side's proof can never be
    played back as the other side's.
    """
    return hmac.new(str(token).encode("utf-8"), f"{role}|{first}|{second}".encode("utf-8"), hashlib.sha256).hexdigest()


def proof_matches(token: str, role: str, first: str, second: str, given: Any) -> bool:
    if not token or not isinstance(given, str):
        return False
    return hmac.compare_digest(proof(token, role, first, second), given)


# The token of the agent last launched per session, kept by the launcher (same process as the
# server).  The server needs the token itself to answer the challenge; the orchestrator's
# ``validate`` callback still decides whether that token is the current one for the session.
_launched: dict[int, str] = {}
_launched_lock = threading.Lock()


def remember_launch(session_id: int, token: str) -> None:
    with _launched_lock:
        _launched[int(session_id)] = str(token)


def launched_token(session_id: int) -> Optional[str]:
    with _launched_lock:
        return _launched.get(int(session_id))


class LineTooLong(ValueError):
    """A line longer than the limit was thrown away; the stream is in step again after it."""


class _LineReader:
    """Newline-delimited reader over a socket that survives timeouts.

    (``socket.makefile`` cannot be used for this: after one timeout every later
    read raises "cannot read from timed out object".)  ``readline`` returns the
    next line without its newline, ``None`` when the peer closed, and lets
    ``socket.timeout`` through with everything read so far kept for the next call.
    A line longer than ``limit`` is an error.  Normally the stream cannot be
    trusted after it (``ValueError``); with ``skip`` the rest of that line is
    read and thrown away without being kept, then :class:`LineTooLong` is raised
    and the next call returns the line after it.  ``deadline`` (``time.monotonic``)
    bounds the whole call rather than each receive.
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()
        self._scanned = 0
        self._skipping = False
        self._skipped = 0

    def readline(self, limit: int, deadline: Optional[float] = None, skip: bool = False) -> Optional[str]:
        while True:
            idx = self._buf.find(b"\n", self._scanned)
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                self._scanned = 0
                if self._skipping:
                    size, self._skipping, self._skipped = self._skipped + len(line), False, 0
                    raise LineTooLong(f"a line of {size} bytes (limit {limit})")
                if len(line) > limit:
                    if skip:
                        raise LineTooLong(f"a line of {len(line)} bytes (limit {limit})")
                    raise ValueError("line too long")
                return line.decode("utf-8", "replace")
            if self._skipping:
                self._skipped += len(self._buf)
                del self._buf[:]
                self._scanned = 0
                if self._skipped > MAX_SKIP:
                    raise ValueError("line far too long")
            else:
                self._scanned = len(self._buf)
                if len(self._buf) > limit:
                    if not skip:
                        raise ValueError("line too long")
                    self._skipping, self._skipped = True, len(self._buf)
                    del self._buf[:]
                    self._scanned = 0
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout("deadline reached")
                self._sock.settimeout(left)
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk


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
                 on_message: Optional[Callable[[int, dict[str, Any]], None]] = None,
                 secret: Optional[Callable[[int], Optional[str]]] = None):
        self.host, self.port = host, port
        self._on_report, self._validate = on_report, validate
        # the launch token of a session (to answer the challenge with); validate() must agree it is current
        self._secret = secret or launched_token
        self._on_connect, self._on_disconnect = on_connect, on_disconnect
        self._on_message = on_message
        self.idle_timeout = idle_timeout
        self._conns: dict[int, _Conn] = {}
        self._lock = threading.Lock()
        self._pending = 0   # connections still in their hello (under _lock)
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            srv.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
            srv.listen(16)
        except OSError:
            srv.close()   # nothing is left behind: start() may be called again later
            raise
        srv.settimeout(1.0)
        self._stop.clear()
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
            with self._lock:
                crowded = self._pending >= MAX_PENDING
                if not crowded:
                    self._pending += 1
            if crowded:   # somebody is opening connections without saying hello; the real agents retry
                sock.close()
                continue
            try:
                threading.Thread(target=self._serve, args=(sock,), name="agent-conn", daemon=True).start()
            except RuntimeError:   # no thread to be had
                self._hello_over()
                sock.close()

    def _hello_over(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def _reject(self, sock: socket.socket, session_id: int, why: str) -> None:
        log.warning("rejected agent connection for session %s (%s)", session_id, why)
        sock.settimeout(SOCKET_TIMEOUT)
        _send_line(sock, threading.Lock(), {"type": "hello_rejected"})

    def _hello(self, sock: socket.socket, reader: _LineReader) -> Optional[_Conn]:
        """The challenge-response hello, within ``HELLO_TIMEOUT`` for all of it; None when it is no good."""
        deadline = time.monotonic() + HELLO_TIMEOUT
        try:
            hello_line = reader.readline(MAX_LINE, deadline=deadline)
            hello = json.loads(hello_line) if hello_line else {}
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                return None
            session_id = int(hello.get("session_id", -1))
            pid = int(hello.get("pid", 0) or 0)
            agent_nonce = hello.get("nonce")
            if session_id < 0:
                return None
            token = self._secret(session_id)
            valid = bool(token) and bool(self._validate(session_id, str(token)))
        except (TypeError, ValueError, OverflowError):   # not JSON, a session id that is no number, an odd pid
            return None
        if not valid:
            self._reject(sock, session_id, "no agent of this service runs there")
            return None
        if not good_nonce(agent_nonce):
            # an agent of an older version (it sends its token instead): it leaves, and the
            # service starts a current one in its place
            self._reject(sock, session_id, "an agent of an older version")
            return None
        token = str(token)
        service_nonce = new_nonce()
        sock.settimeout(SOCKET_TIMEOUT)
        if not _send_line(sock, threading.Lock(), {"type": "challenge", "nonce": service_nonce,
                                                   "proof": proof(token, "service", agent_nonce, service_nonce)}):
            return None
        try:
            auth_line = reader.readline(MAX_LINE, deadline=deadline)
            auth = json.loads(auth_line) if auth_line else {}
        except ValueError:
            return None
        if not isinstance(auth, dict) or auth.get("type") != "auth" or \
                not proof_matches(token, "agent", service_nonce, agent_nonce, auth.get("proof")):
            self._reject(sock, session_id, "it could not prove the launch token")
            return None
        # the name is only for the log; it comes from the agent, so it is cut and stripped of control characters
        user = "".join(ch for ch in str(hello.get("user", ""))[:64] if ch.isprintable())
        return _Conn(sock=sock, session_id=session_id, user=user, pid=pid)

    def _serve(self, sock: socket.socket) -> None:
        conn: Optional[_Conn] = None
        try:
            reader = _LineReader(sock)
            try:
                conn = self._hello(sock, reader)
            finally:
                self._hello_over()
            if conn is None:
                return
            session_id = conn.session_id
            with self._lock:
                old = self._conns.get(session_id)
                self._conns[session_id] = conn
            if old is not None:
                try:
                    old.sock.close()
                except OSError:
                    pass
            sock.settimeout(SOCKET_TIMEOUT)
            _send_line(sock, conn.lock, {"type": "hello_ok", "session_id": session_id})
            log.info("agent connected: session %s user %r pid %s", session_id, conn.user, conn.pid)
            if self._on_connect:
                self._on_connect(session_id, conn.user, conn.pid)
            pinged = False
            while not self._stop.is_set():
                try:
                    line = reader.readline(MAX_LINE_LARGE, skip=True)
                except LineTooLong as exc:
                    # e.g. a screenshot that came out larger than a message may be: say so, to the
                    # log and to the agent, and carry on with the connection
                    pinged = False
                    conn.last_seen = time.time()
                    log.warning("session %s sent %s; ignored", session_id, exc)
                    _send_line(sock, conn.lock, {"type": "error", "error": "message_too_large", "limit": MAX_LINE_LARGE})
                    if self._on_message is not None:
                        try:
                            self._on_message(session_id, {"type": "oversized", "limit": MAX_LINE_LARGE})
                        except Exception:  # noqa: BLE001
                            log.exception("bad oversized notice for session %s", session_id)
                    continue
                except socket.timeout:
                    quiet = time.time() - conn.last_seen
                    if quiet > self.idle_timeout * 1.5:
                        break  # no sign of life, not even an answer to the ping
                    if quiet > self.idle_timeout and not pinged:
                        pinged = True
                        if not _send_line(sock, conn.lock, {"type": "ping"}):
                            break
                    continue
                if line is None:
                    break
                if not line.strip():
                    continue
                pinged = False
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
                    except Exception:  # noqa: BLE001 - from_dict takes anything; this is for the callback
                        log.exception("report from session %s could not be handled", session_id)
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
                 reconnect_seconds: float = 5.0, token: Optional[str] = None):
        self.host, self.port = host, port
        hello = dict(hello)
        # the token is never sent: it only signs the answers of the hello (a "token" in the
        # hello dict is taken out and used for that)
        self._token = str(token if token is not None else hello.get("token") or "")
        hello.pop("token", None)
        self.hello = dict(hello, type="hello", proto=PROTOCOL)
        self._bad_proofs = 0
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

    def _handshake(self, sock: socket.socket, reader: _LineReader) -> Optional[dict[str, Any]]:
        """The agent's side of the hello: the ``hello_ok``, or None when the service rejected this agent.

        Raises (and the connection is tried again later) when the other end is not
        the service or did not answer as it does.  Nothing it sends is acted on
        before it has proved that it knows the launch token.
        """
        deadline = time.monotonic() + HELLO_TIMEOUT * 1.5

        def read() -> Any:
            line = reader.readline(MAX_LINE, deadline=deadline)
            if line is None:
                raise OSError("the service closed the connection")
            return json.loads(line) if line.strip() else line

        def rejection(resp: Any) -> bool:
            if isinstance(resp, dict) and resp.get("type") == "hello_rejected":
                log.error("service rejected this agent; giving up")
                return True
            return False

        nonce = new_nonce()
        if not _send_line(sock, self._send_lock, dict(self.hello, nonce=nonce)):
            raise OSError("hello failed")
        resp = read()
        if rejection(resp):
            return None
        if not isinstance(resp, dict) or resp.get("type") != "challenge":
            # not the service's answer (it closed first, or something else has the port): try again
            raise ValueError(f"unexpected reply to hello: {str(resp)[:80]!r}")
        service_nonce = resp.get("nonce")
        if not good_nonce(service_nonce) or not proof_matches(self._token, "service", nonce, service_nonce, resp.get("proof")):
            self._bad_proofs += 1
            log.warning("whatever answers on %s:%s cannot prove it is the service that started this agent; "
                        "not trusted (%d)", self.host, self.port, self._bad_proofs)
            if self._bad_proofs >= MAX_BAD_PROOFS:
                # the service there has another token for this session: this agent is a stale one
                log.error("the service keeps not knowing this agent's token; giving up")
                return None
            raise ValueError("bad proof from the service")
        self._bad_proofs = 0
        if not _send_line(sock, self._send_lock, {"type": "auth", "proof": proof(self._token, "agent", service_nonce, nonce)}):
            raise OSError("auth failed")
        resp = read()
        if rejection(resp):
            return None
        if not isinstance(resp, dict) or resp.get("type") != "hello_ok":
            raise ValueError(f"unexpected reply to auth: {str(resp)[:80]!r}")
        return resp

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                self._sock = sock
                reader = _LineReader(sock)
                resp = self._handshake(sock, reader)
                if resp is None:
                    self.rejected.set()
                    return
                sock.settimeout(90)
                self.connected.set()
                log.info("connected to service at %s:%s", self.host, self.port)
                try:
                    self._on_message(resp)   # lets the agent start afresh on every (re)connect
                except Exception:  # noqa: BLE001
                    log.exception("message handler failed")
                while not self._stop.is_set():
                    try:
                        line = reader.readline(MAX_LINE, skip=True)
                    except LineTooLong as exc:
                        log.warning("the service sent %s; ignored", exc)
                        continue
                    except socket.timeout:
                        if not _send_line(sock, self._send_lock, {"type": "ping"}):
                            break
                        continue
                    if line is None:
                        break
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict):
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
            except Exception:  # noqa: BLE001 - whatever it was, the agent must come back
                log.exception("connection to service failed unexpectedly; reconnecting")
            finally:
                self.connected.clear()
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            self._stop.wait(self.reconnect_seconds)
