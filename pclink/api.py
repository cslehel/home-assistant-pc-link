"""Optional local REST + WebSocket API (stdlib only) and the setup web page.

The only link to Home Assistant; off by default.  Bound to ``127.0.0.1`` unless a token is
configured.  Endpoints (all JSON):

    GET  /                      setup / status page (HTML; holds no data itself)
    GET  /api/health            liveness
    GET  /api/auth              what the presented token may do (token)
    GET  /api/status            PC payload
    GET  /api/users             every user's payload
    GET  /api/users/<user>      one user
    GET  /api/users/<user>/screenshot   last screenshot image (token)
    GET  /api/users/<user>/report?hours=10   apps and websites used in the last N hours
    GET  /api/entities          entity catalog for the native HA integration
    GET  /api/events?n=50       recent events (&since=<ts>; &announced=1: the pushed ones)
    GET  /api/config            config.yaml text          (admin)
    PUT  /api/config            validate + write + reload (admin)
    POST /api/command           JSON commands (token)
    POST /api/reload            hot-reload config.yaml    (admin)
    GET  /api/ws                WebSocket: pushes {"type":"state",...} on change,
                                accepts the same JSON commands as /api/command

Authentication: ``Authorization: Bearer <token>`` or ``?token=``.  Reads from
loopback need no token; every write does.  Without a token configured the API
is read-only.  A non-loopback bind requires a token for *everything*.

``api.read_token`` is a second token that can only read (never a command, the
configuration or a screenshot).  Once it is set, reads from loopback need a
token as well - it or the main one - so that other users logged in on the PC
cannot look at each other's usage.

"Admin" is what amounts to running code as SYSTEM: the configuration (its
``commands:``), reloading it, the configured custom commands, installing a
PC Link update and a ``launch`` with a free ``command`` (run through the shell in
every active session, an administrator's too; launching a configured app by
name is everyday control).  With ``api.admin_token`` set only that token may do these and
``api.token`` is left with everyday control; without it ``api.token`` may, and
the configuration additionally needs ``api.allow_config_edit``.

With ``api.tls_cert`` / ``api.tls_key`` the API is served over HTTPS
(``pclink make-cert`` creates a pair; Home Assistant pins its fingerprint).

What keeps other parties out besides the tokens: the port is bound exclusively
(nothing else on the PC can listen on it next to the service), requests without
a token are only answered under a host name that is this PC's (so a web page
cannot reach them through DNS rebinding), wrong tokens are throttled per
address, and connections are capped and given a deadline for their request.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import queue
import re
import select
import socket
import ssl
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .config import ApiConfig

log = logging.getLogger(__name__)

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_MESSAGE = 4 * 1024 * 1024
MAX_WS_CLIENTS = 32
MAX_WS_ANONYMOUS = 8      # of those, at most this many without a token (programs on the PC itself)
WS_QUEUE = 64             # frames waiting for one client before it is considered stuck
WS_SEND_TIMEOUT = 15.0    # seconds one frame may take to leave
MAX_BODY = 2 * 1024 * 1024
MAX_CONNECTIONS = 96                # open connections in all; every one is a thread
MAX_CONNECTIONS_PER_ADDRESS = 40    # of those without a token (so far), from one address (the PC itself counts as one)
TOKEN_RESERVE = 16                  # of MAX_CONNECTIONS, kept for connections that show a token ...
TOKEN_RESERVE_PER_ADDRESS = 4       # ... at most this many of them waiting for theirs from one address
IDLE_TIMEOUT = 60.0       # seconds a connection may wait for its next request
HEAD_TIMEOUT = 15.0       # seconds a request line with its headers (and a TLS handshake) may take in all
BODY_TIMEOUT = 60.0       # seconds a request body may take in all
AUTH_FAILURES = 10        # wrong tokens from one address within AUTH_WINDOW seconds ...
AUTH_WINDOW = 60.0
AUTH_LOCKOUT = 60.0       # ... after which tokens from that address are not looked at for this long

# access levels of a token
NONE, READ, WRITE, ADMIN = 0, 1, 2, 3
THROTTLED = -1

# JSON commands that amount to running code as SYSTEM (see the module text)
ADMIN_ACTIONS = ("pclink_update_install", "run", "reload")
ADMIN_ENTITIES = ("pclink_update_install", "reload_config")


def is_admin_command(data: dict[str, Any]) -> bool:
    """Does this JSON command need the admin level (update install, custom command, reload,
    a ``launch`` with a command line of its own)?"""
    action = str(data.get("action", "")).lower()
    if action in ADMIN_ACTIONS:
        return True
    if action == "launch":
        # A free ``command`` is run by the agents through the shell in every active session -
        # an administrator's too.  Starting a configured app by name stays everyday control.
        command = data.get("command")
        return command not in (None, "", False)
    if action == "set":
        entity = str(data.get("entity", "") or "")
        return entity in ADMIN_ENTITIES or entity.startswith("cmd_")
    return False


def address_bucket(address: str) -> str:
    """The key under which connections and wrong tokens from ``address`` are counted.

    Every address of the PC itself (all of 127.0.0.0/8, ``::1``, their IPv4-mapped forms) is
    one: a program on the PC could otherwise use a fresh 127.x address for every few
    connections or guesses.  An IPv6 address counts by its /64, which one device can fill
    with addresses of its own."""
    text = str(address or "")
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return text
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def cert_fingerprint(path: Optional[str]) -> Optional[str]:
    """SHA-256 of the (first) certificate in a PEM file, lower-case hex; None when it cannot be read."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            text = fh.read()
        match = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
        if not match:
            return None
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(match.group(0))).hexdigest()
    except (OSError, ValueError):
        return None


class ApiBackend:
    """What the orchestrator implements for the API (duck-typed; see core.py)."""

    def api_health(self) -> dict[str, Any]: ...
    def api_status(self) -> dict[str, Any]: ...
    def api_users(self) -> dict[str, dict[str, Any]]: ...
    def api_user(self, user: str) -> Optional[dict[str, Any]]: ...
    def api_events(self, n: int, user: Optional[str], since: Optional[float] = None,
                   announced: bool = False) -> list[dict[str, Any]]: ...
    def api_report(self, user: str, hours: Optional[float]) -> Optional[dict[str, Any]]: ...
    def api_entities(self) -> dict[str, Any]: ...
    def api_screenshot(self, user: str) -> Optional[tuple[bytes, str]]: ...
    def api_command(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def api_config_text(self) -> str: ...
    def api_config_write(self, text: str) -> dict[str, Any]: ...
    def api_reload(self) -> dict[str, Any]: ...


class AuthThrottle:
    """Slows down guessing a token: after a number of wrong ones from an address, tokens from
    that address are refused unseen for a while (a right one too; the legitimate client is on
    another address, or simply waits)."""

    def __init__(self, failures: int = AUTH_FAILURES, window: float = AUTH_WINDOW, lockout: float = AUTH_LOCKOUT,
                 clock: Callable[[], float] = time.monotonic):
        self.failures, self.window, self.lockout = failures, window, lockout
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: dict[str, list[float]] = {}     # address -> times of recent wrong tokens
        self._until: dict[str, float] = {}          # address -> locked out until

    def blocked(self, address: str) -> float:
        """Seconds this address still has to wait (0: it may try).  Addresses are counted by
        :func:`address_bucket`: the PC itself is one, whichever 127.x it connects from."""
        address = address_bucket(address)
        with self._lock:
            left = self._until.get(address, 0.0) - self._clock()
            if left <= 0:
                self._until.pop(address, None)
                return 0.0
            return left

    def failed(self, address: str) -> bool:
        """Note a wrong token; True when this one started a lockout."""
        address = address_bucket(address)
        now = self._clock()
        with self._lock:
            if len(self._seen) > 4096:   # never grows without bound
                self._seen.clear()
            recent = [t for t in self._seen.get(address, []) if now - t < self.window] + [now]
            if len(recent) >= self.failures:
                self._seen.pop(address, None)
                self._until[address] = now + self.lockout
                return True
            self._seen[address] = recent
            return False


class _WsProtocolError(Exception):
    """The client broke the WebSocket protocol; ``code`` is the close code to answer with."""

    def __init__(self, message: str, code: int = 1002):
        super().__init__(message)
        self.code = code


class _WsClient:
    """One WebSocket connection.  Frames are queued and written by the client's own
    thread, so a client that stops reading can never block the caller (the service's
    main loop pushes state through here); a client whose queue fills up is dropped."""

    def __init__(self, sock: socket.socket, lock: Optional[threading.Lock] = None, anonymous: bool = False):
        self.sock = sock
        self.alive = True
        self.anonymous = anonymous     # connected without a token: takes one of the few slots for those
        self._io_lock = lock           # TLS: reading and writing take turns on the connection
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=WS_QUEUE)
        self._writer = threading.Thread(target=self._write_loop, name="api-ws-writer", daemon=True)
        self._writer.start()

    def send_text(self, text: str) -> bool:
        return self.send(0x1, text.encode("utf-8"))

    def send(self, opcode: int, payload: bytes) -> bool:
        if not self.alive:
            return False
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
            self._queue.put_nowait(bytes(head) + payload)
            return True
        except queue.Full:
            log.warning("api: dropping a WebSocket client that is not reading")
            self.close()
            return False

    def close(self) -> None:
        self.alive = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _write_loop(self) -> None:
        while self.alive:
            try:
                frame = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if frame is None:
                break
            try:
                _send_all(self.sock, frame, WS_SEND_TIMEOUT, self._io_lock)
            except (OSError, ValueError):  # ValueError: the socket was closed under us
                break
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


def _send_all(sock: socket.socket, data: bytes, timeout: float, lock: Optional[threading.Lock] = None) -> None:
    """``sendall`` with a deadline that does not disturb the reader's ``recv``."""
    deadline = time.monotonic() + timeout
    view = memoryview(data)
    chunk = 16 * 1024 if lock is not None else 256 * 1024   # TLS: one record at a time
    while view:
        left = deadline - time.monotonic()
        if left <= 0:
            raise OSError("send timed out")
        _r, writable, _x = select.select([], [sock], [], left)
        if not writable:
            raise OSError("send timed out")
        if lock is None:
            sent = sock.send(view[:chunk])
        else:
            with lock:
                try:
                    sent = sock.send(view[:chunk])
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    sent = 0
        view = view[sent:]


def _unmask(data: bytes, mask: bytes) -> bytes:
    """XOR ``data`` with the 4-byte mask (as two big integers: megabytes in milliseconds)."""
    n = len(data)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class _WsReader:
    """Reads client messages from a file-like, by the rules of RFC 6455.

    ``read`` returns ``(opcode, payload)`` for a complete text / binary message or
    a control frame, None when the connection ended, and raises
    :class:`_WsProtocolError` on what a client must not send: unmasked frames,
    reserved bits, fragmented or long control frames, a continuation of nothing,
    a new message inside an unfinished one, more than ``MAX_WS_MESSAGE``.  A control
    frame between the fragments of a message is handed out at once and the
    message goes on after it."""

    def __init__(self, stream):
        self._stream = stream
        self._opcode: Optional[int] = None
        self._parts: list[bytes] = []
        self._size = 0

    def _exact(self, n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            chunk = self._stream.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def read(self) -> Optional[tuple[int, bytes]]:
        while True:
            head = self._exact(2)
            if head is None:
                return None
            fin = bool(head[0] & 0x80)
            op = head[0] & 0x0F
            length = head[1] & 0x7F
            control = op >= 0x8
            if head[0] & 0x70:
                raise _WsProtocolError("reserved bits set")
            if op not in (0x0, 0x1, 0x2, 0x8, 0x9, 0xA):
                raise _WsProtocolError(f"unknown opcode {op}")
            if not head[1] & 0x80:
                raise _WsProtocolError("client frames must be masked")
            if control and (not fin or length > 125):
                raise _WsProtocolError("control frames are short and never fragmented")
            if length == 126:
                ext = self._exact(2)
                if ext is None:
                    return None
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._exact(8)
                if ext is None:
                    return None
                length = struct.unpack("!Q", ext)[0]
            if length > MAX_WS_MESSAGE or (not control and self._size + length > MAX_WS_MESSAGE):
                raise _WsProtocolError("message too large", 1009)
            mask = self._exact(4)
            if mask is None:
                return None
            data = self._exact(length) if length else b""
            if data is None:
                return None
            data = _unmask(data, mask)
            if control:
                return op, data
            if op == 0x0:
                if self._opcode is None:
                    raise _WsProtocolError("continuation without a message")
            elif self._opcode is not None:
                raise _WsProtocolError("new message before the last one ended")
            else:
                self._opcode = op
            self._parts.append(data)
            self._size += length
            if fin:
                opcode, message = self._opcode, b"".join(self._parts)
                self._opcode, self._parts, self._size = None, [], 0
                return opcode, message


def _ws_read_frame(stream) -> Optional[tuple[int, bytes]]:
    """Read one client message or control frame from a file-like; None on close or error."""
    try:
        return _WsReader(stream).read()
    except _WsProtocolError:
        return None


class _Reader(io.RawIOBase):
    """The handler's raw read side of the connection.

    While HTTP is spoken it enforces the deadlines: a connection may idle for
    ``IDLE_TIMEOUT`` between requests, but once a request has started its head has
    ``HEAD_TIMEOUT`` in all (a slow sender cannot hold a thread by trickling
    bytes).  After a WebSocket upgrade (:meth:`to_websocket`) it waits without a
    timeout until data arrives or the client is gone, sharing the connection with
    the writer thread through ``lock`` where that is needed (TLS)."""

    def __init__(self, sock: socket.socket):
        super().__init__()
        self._sock = sock
        self.deadline: Optional[float] = None
        self.awaiting_request = False
        self._alive: Optional[Callable[[], bool]] = None
        self._lock: Optional[threading.Lock] = None

    def readable(self) -> bool:
        return True

    def to_websocket(self, alive: Callable[[], bool], lock: Optional[threading.Lock]) -> None:
        self._alive, self._lock = alive, lock
        self.deadline = None

    def readinto(self, buf) -> int:
        if self._alive is not None:
            return self._read_websocket(buf)
        timeout = IDLE_TIMEOUT
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the request took too long")
            timeout = min(timeout, left)
        self._sock.settimeout(timeout)
        n = self._sock.recv_into(buf)
        if n and self.awaiting_request:   # a request has begun: the rest of its head is on the clock
            self.awaiting_request = False
            self.deadline = time.monotonic() + HEAD_TIMEOUT
        return n

    def _read_websocket(self, buf) -> int:
        sock = self._sock
        while self._alive():
            try:
                pending = sock.pending() if isinstance(sock, ssl.SSLSocket) else 0
                if not pending and not select.select([sock], [], [], 1.0)[0]:
                    continue
                if self._lock is None:
                    return sock.recv_into(buf)
                with self._lock:
                    sock.settimeout(WS_SEND_TIMEOUT)
                    try:
                        return sock.recv_into(buf)
                    except (TimeoutError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        continue   # half a TLS record so far; nothing was lost
            except (OSError, ValueError):   # closed, by the peer or by us
                return 0
        return 0


# what a connection is counted as (see _Caps)
SLOT_OPEN, SLOT_PENDING, SLOT_TOKEN = "open", "pending", "token"


class _Slot:
    """One admitted connection: where it comes from and what it is counted as."""

    __slots__ = ("bucket", "state")

    def __init__(self, bucket: str, state: str):
        self.bucket, self.state = bucket, state


class _Caps:
    """The connection counts of one API, shared by its listeners (IPv4 and IPv6).

    A connection without a token takes an ordinary place: at most ``MAX_CONNECTIONS -
    TOKEN_RESERVE`` of those, and ``MAX_CONNECTIONS_PER_ADDRESS`` from one address (the PC
    itself counts as one, :func:`address_bucket`).  When those are taken, a new connection may
    still have a place in the reserve - but only to present a token with its first request,
    within ``HEAD_TIMEOUT``; without one it is answered 503 and closed.  A connection that has
    shown a valid token leaves the ordinary counts, so a flood of connections without one
    cannot keep Home Assistant out."""

    def __init__(self):
        self.lock = threading.Lock()
        self.open = 0                                  # every connection
        self.anonymous = 0                             # ordinary places taken
        self.per_address: dict[str, int] = {}          # ordinary places by address
        self.pending = 0                               # reserve places waiting for a token
        self.pending_per_address: dict[str, int] = {}

    @staticmethod
    def _down(counts: dict[str, int], key: str) -> None:
        left = counts.get(key, 0) - 1
        if left > 0:
            counts[key] = left
        else:
            counts.pop(key, None)

    def admit(self, address: str) -> Optional[_Slot]:
        bucket = address_bucket(address)
        with self.lock:
            if self.open >= MAX_CONNECTIONS:
                return None
            if (self.anonymous < MAX_CONNECTIONS - TOKEN_RESERVE
                    and self.per_address.get(bucket, 0) < MAX_CONNECTIONS_PER_ADDRESS):
                self.anonymous += 1
                self.per_address[bucket] = self.per_address.get(bucket, 0) + 1
                slot = _Slot(bucket, SLOT_OPEN)
            elif self.pending < TOKEN_RESERVE and self.pending_per_address.get(bucket, 0) < TOKEN_RESERVE_PER_ADDRESS:
                self.pending += 1
                self.pending_per_address[bucket] = self.pending_per_address.get(bucket, 0) + 1
                slot = _Slot(bucket, SLOT_PENDING)
            else:
                return None
            self.open += 1
            return slot

    def _release(self, slot: _Slot) -> None:
        if slot.state == SLOT_OPEN:
            self.anonymous = max(0, self.anonymous - 1)
            self._down(self.per_address, slot.bucket)
        elif slot.state == SLOT_PENDING:
            self.pending = max(0, self.pending - 1)
            self._down(self.pending_per_address, slot.bucket)

    def promote(self, slot: _Slot) -> None:
        """The connection showed a valid token: it no longer counts against those without."""
        with self.lock:
            if slot.state != SLOT_TOKEN:
                self._release(slot)
                slot.state = SLOT_TOKEN

    def leave(self, slot: _Slot) -> None:
        with self.lock:
            self._release(slot)
            slot.state = ""
            self.open = max(0, self.open - 1)


class _Server(ThreadingHTTPServer):
    """One listener: an exclusively bound port, a cap on connections (shared with the API's
    other listener, see :class:`_Caps`), optionally TLS."""

    daemon_threads = True
    # On Windows SO_REUSEADDR does not mean "rebind after a restart" but "others may bind
    # this port too": any program on the PC could then listen on a more specific address
    # and be handed the clients, with their tokens.  SO_EXCLUSIVEADDRUSE is the opposite.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, handler, tls: Optional[ssl.SSLContext] = None,
                 family: int = socket.AF_INET, caps: Optional[_Caps] = None):
        self._tls = tls
        self.address_family = family
        self.caps = caps if caps is not None else _Caps()
        self._slots: dict[Any, _Slot] = {}
        super().__init__(address, handler)
        if tls is not None:
            # the handshake is done by the connection's own thread, never by the accepting one
            self.socket = tls.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        if self.address_family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
            # IPv6 only: the IPv4 side has a listener of its own
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()

    def slot(self, request) -> Optional[_Slot]:
        return self._slots.get(request)

    def process_request(self, request, client_address) -> None:
        address = client_address[0]
        slot = self.caps.admit(address)
        if slot is None:
            log.debug("api: too many connections; refusing one from %s", address)
            self.shutdown_request(request)
            return
        self._slots[request] = slot
        try:
            super().process_request(request, client_address)
        except Exception:  # noqa: BLE001 - no thread could be started
            self._slots.pop(request, None)
            self.caps.leave(slot)
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            slot = self._slots.pop(request, None)
            if slot is not None:
                self.caps.leave(slot)

    def handle_error(self, request, client_address) -> None:
        log.debug("api: connection from %s failed", client_address[0], exc_info=True)


def listen_plan(host: str) -> list[tuple[int, str, bool]]:
    """Where the API listens for ``api.host``: ``(family, address, required)``.

    Next to a loopback or wildcard address the same port is also taken on the other IP
    version: ``localhost`` may resolve to ``::1`` (Home Assistant on the same PC, a browser),
    and a program of a monitored user listening on ``[::1]:<port>`` would otherwise be handed
    those clients with their token.  The second one is not required: a PC without IPv6
    keeps its API."""
    text = str(host or "").strip().lower()
    if text in ("127.0.0.1", "localhost"):
        return [(socket.AF_INET, "127.0.0.1", True), (socket.AF_INET6, "::1", False)]
    if text == "::1":
        return [(socket.AF_INET6, "::1", True), (socket.AF_INET, "127.0.0.1", False)]
    if text in ("0.0.0.0", ""):
        return [(socket.AF_INET, "0.0.0.0", True), (socket.AF_INET6, "::", False)]
    if text == "::":
        return [(socket.AF_INET6, "::", True), (socket.AF_INET, "0.0.0.0", False)]
    try:
        ip = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return [(socket.AF_INET, text, True)]   # a host name: as before
    if ip.version == 6:
        return [(socket.AF_INET6, str(ip), True)]
    return [(socket.AF_INET, str(ip), True)]


# errors that mean "no IPv6 here" (the companion listener is then left out); anything else -
# above all the port being taken on that address by somebody else - stops the start
_NO_IPV6 = {getattr(errno, name) for name in ("EAFNOSUPPORT", "EADDRNOTAVAIL", "EPROTONOSUPPORT") if hasattr(errno, name)} \
    | {10047, 10049, 10043}   # WSAEAFNOSUPPORT, WSAEADDRNOTAVAIL, WSAEPROTONOSUPPORT


def _tls_handshake(sock: "ssl.SSLSocket", seconds: float) -> bool:
    """The TLS handshake with a deadline for the whole of it."""
    deadline = time.monotonic() + seconds
    sock.settimeout(0.0)
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                sock.do_handshake()
                return True
            except ssl.SSLWantReadError:
                select.select([sock], [], [], left)
            except ssl.SSLWantWriteError:
                select.select([], [sock], [], left)
    except (OSError, ValueError):   # not TLS at all, an unknown certificate authority, a closed connection
        return False
    finally:
        try:
            sock.settimeout(IDLE_TIMEOUT)
        except OSError:
            pass


class ApiServer:
    def __init__(self, cfg: ApiConfig, backend: ApiBackend, html: str = ""):
        self.cfg = cfg
        self.backend = backend
        self.html = html
        self._server: Optional[_Server] = None
        self._extra: list[_Server] = []          # the same port on the other IP version (listen_plan)
        self._thread: Optional[threading.Thread] = None
        self._threads: list[threading.Thread] = []
        self._clients: list[_WsClient] = []
        self._clients_lock = threading.Lock()
        self.started_at = time.time()
        self.throttle = AuthThrottle()
        self.host_names: set[str] = {"localhost"}
        self.page_headers = self._page_headers(html)

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else self.cfg.port

    @property
    def tls(self) -> bool:
        return bool(getattr(self.cfg, "tls_cert", None) and getattr(self.cfg, "tls_key", None))

    def start(self) -> None:
        api = self

        class Handler(_Handler):
            server_api = api

        if self._server is not None:   # started again (the service retries a start that failed): never two listeners
            return
        context = None
        if self.tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.cfg.tls_cert, self.cfg.tls_key)
        self.host_names = self._own_names()
        caps = _Caps()
        servers: list[_Server] = []
        try:
            for family, address, required in listen_plan(self.cfg.host):
                port = servers[0].server_address[1] if servers else self.cfg.port
                try:
                    servers.append(_Server((address, port), Handler, tls=context, family=family, caps=caps))
                except OSError as exc:
                    if required or not ({exc.errno, getattr(exc, "winerror", None)} & _NO_IPV6):
                        raise
                    log.info("local API: not listening on %s as well (%s)", address, exc)
        except BaseException:
            for server in servers:
                server.server_close()
            raise
        self._server, self._extra = servers[0], servers[1:]
        self._threads = []
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, name="api", daemon=True)
            thread.start()
            self._threads.append(thread)
        self._thread = self._threads[0]
        log.info("local API listening on %s", ", ".join(
            "%s://%s:%s/" % ("https" if context else "http",
                             "[%s]" % s.server_address[0] if s.address_family == socket.AF_INET6 else s.server_address[0],
                             s.server_address[1]) for s in servers))

    @property
    def caps(self) -> Optional[_Caps]:
        """The connection counts (None before the start)."""
        return self._server.caps if self._server else None

    def stop(self) -> None:
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            c.send(0x8, b"")
            c.close()
        for server in ([self._server] if self._server else []) + list(self._extra):
            server.shutdown()
            server.server_close()
        self._server, self._extra = None, []

    def _own_names(self) -> set[str]:
        """Host names under which this PC may be addressed without a token (see ``host_allowed``)."""
        names = {"localhost", str(self.cfg.host).lower()}
        try:
            host = socket.gethostname().lower()
            names.update({host, host.split(".")[0], host.split(".")[0] + ".local"})
            names.add(socket.getfqdn().lower())
        except OSError:
            pass
        for extra in getattr(self.cfg, "allowed_hosts", None) or []:
            names.add(str(extra).strip().lower().rstrip("."))
        names.discard("")
        return names

    def host_allowed(self, header: Optional[str]) -> bool:
        """Is the request's ``Host`` this PC?  An IP address, ``localhost`` or one of its own names.

        A web page somewhere else can make its own host name point at 127.0.0.1 ("DNS
        rebinding") and so talk to this API from the browser of somebody at the PC; the
        name it has to use for that is not one of these.  Requests that carry a valid
        token are not held to this (such a page has no token): a PC that is reached
        under a name of the home router keeps working for Home Assistant."""
        host = (header or "").strip().lower()
        if not host:
            return True   # HTTP/1.0 and hand-made clients; a browser always sends it
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        host = host.rstrip(".")
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return host in self.host_names

    @staticmethod
    def _page_headers(html: str) -> dict[str, str]:
        """Headers for the setup page: it may not be framed, and runs only the script it came with."""
        hashes = " ".join(
            "'sha256-%s'" % base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
            for script in re.findall(r"<script>(.*?)</script>", html or "", re.S))
        return {
            "Content-Security-Policy": ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; connect-src 'self'; "
                                        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                                        % (hashes or "'none'")),
            "X-Frame-Options": "DENY",
        }

    # -- auth ----------------------------------------------------------------------------------
    @staticmethod
    def _same(token: Optional[str], secret: Optional[str]) -> bool:
        if not secret or not token:
            return False
        return hmac.compare_digest(str(token).encode("utf-8"), str(secret).encode("utf-8"))

    @property
    def admin_token(self) -> Optional[str]:
        return getattr(self.cfg, "admin_token", None) or None

    def token_level(self, token: Optional[str]) -> int:
        """What a token may do: ``ADMIN``, ``WRITE``, ``READ`` or ``NONE`` (every comparison in constant time)."""
        admin = self._same(token, self.admin_token)
        write = self._same(token, self.cfg.token)
        read = self._same(token, self.cfg.read_token)
        if admin:
            return ADMIN
        if write:
            return WRITE if self.admin_token else ADMIN
        return READ if read else NONE

    def check(self, token: Optional[str], address: str) -> int:
        """``token_level`` behind the throttle: ``THROTTLED`` while the address is locked out."""
        if not token:
            return NONE
        if self.throttle.blocked(address) > 0:
            return THROTTLED
        level = self.token_level(token)
        if level == NONE and self.throttle.failed(address):
            log.warning("api: %d wrong tokens from %s; ignoring tokens from there for %d s",
                        self.throttle.failures, address, self.throttle.lockout)
        return level

    def token_ok(self, token: Optional[str]) -> bool:
        """May this token send commands?"""
        return self.token_level(token) >= WRITE

    def read_token_ok(self, token: Optional[str]) -> bool:
        """Is this the read-only token (``api.read_token``)?  It never authorises a write."""
        return self._same(token, self.cfg.read_token)

    def config_refusal(self, level: int) -> Optional[str]:
        """Why this level may not read or write the configuration (None: it may)."""
        if level < ADMIN:
            return "admin token required (api.admin_token)"
        if not self.admin_token and not self.cfg.allow_config_edit:
            return "config editing disabled (api.allow_config_edit)"
        return None

    @property
    def open_local_reads(self) -> bool:
        """May programs on the PC itself read without a token?  Not once a read token is set."""
        return not self.cfg.read_token

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

    def _add_client(self, client: _WsClient) -> bool:
        with self._clients_lock:
            self._clients = [c for c in self._clients if c.alive]
            if len(self._clients) >= MAX_WS_CLIENTS:
                return False
            # connections without a token only get a share, so that they cannot crowd out Home Assistant
            if client.anonymous and sum(1 for c in self._clients if c.anonymous) >= MAX_WS_ANONYMOUS:
                return False
            self._clients.append(client)
            return True

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
    timeout = IDLE_TIMEOUT  # an idle or half-open connection must not hold a thread for ever

    def setup(self) -> None:
        super().setup()
        self._usable = True
        self._auth: Optional[tuple[Any, int]] = None
        slot = getattr(self.server, "slot", None)
        self._slot: Optional[_Slot] = slot(self.request) if slot else None
        # a connection in the reserve has HEAD_TIMEOUT in all to show its token
        self._pending_deadline = time.monotonic() + HEAD_TIMEOUT
        if isinstance(self.connection, ssl.SSLSocket) and not _tls_handshake(self.connection, HEAD_TIMEOUT):
            self._usable = False
        # our own read side, which knows about deadlines (see _Reader)
        try:
            self.rfile.close()
        except OSError:
            pass
        self._reader = _Reader(self.connection)
        self.rfile = io.BufferedReader(self._reader, 64 * 1024)

    def handle(self) -> None:
        if self._usable:
            super().handle()

    @property
    def _pending(self) -> bool:
        return self._slot is not None and self._slot.state == SLOT_PENDING

    def handle_one_request(self) -> None:
        self._reader.deadline = None
        self._reader.awaiting_request = True
        if self._pending:
            self._reader.deadline, self._reader.awaiting_request = self._pending_deadline, False
        self._auth = None
        super().handle_one_request()

    def parse_request(self) -> bool:
        try:
            ok = super().parse_request()
        finally:
            self._reader.deadline = None   # the head is in: what follows has its own limits
        if ok and self._pending:
            level = self._level()          # a valid token moves the connection out of the reserve
            if self._pending:
                self.close_connection = True
                if level == THROTTLED:
                    self._refuse()
                else:
                    self._json(503, {"error": "too many connections; only requests with a token are taken now"},
                               {"Retry-After": "5"})
                return False
        return ok

    def log_message(self, fmt, *args):  # noqa: D401 - quiet
        if log.isEnabledFor(logging.DEBUG):
            path = getattr(self, "path", "") or ""
            text = fmt % args
            log.debug("api: %s", text.replace(path, urlsplit(path).path) if path else text)

    def _same_origin(self) -> bool:
        """Browsers send Origin on WebSocket handshakes and do not apply the same-origin
        policy to them: refuse pages from other sites (clients that are not browsers send none)."""
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        return urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any, extra: Optional[dict[str, str]] = None) -> None:
        self._send(status, json.dumps(data, default=str).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self._send(status, body, content_type)

    def _text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8",
              extra: Optional[dict[str, str]] = None) -> None:
        self._send(status, text.encode("utf-8"), content_type, extra)

    def _page(self) -> None:
        api = self.server_api
        self._text(200, api.html or "<html><body>pclink</body></html>", "text/html; charset=utf-8", api.page_headers)

    def _token(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        query = parse_qs(urlsplit(self.path).query)
        if "token" in query:
            return query["token"][0]
        return self.headers.get("X-Token")

    def _level(self) -> int:
        """The level of this request's token; looked at (and counted when wrong) once per request."""
        if self._auth is None or self._auth[0] is not self.headers:
            self._auth = (self.headers, self.server_api.check(self._token(), self.client_address[0]))
            if self._auth[1] >= READ and self._slot is not None and self._slot.state != SLOT_TOKEN:
                caps = getattr(self.server, "caps", None)
                if caps is not None:
                    caps.promote(self._slot)
        return self._auth[1]

    def _anonymous_ok(self) -> bool:
        """May this request read without a token?  From the PC itself, addressed as the PC."""
        api = self.server_api
        return api.open_local_reads and self._from_this_pc() and api.host_allowed(self.headers.get("Host"))

    def _authorized(self, write: bool) -> bool:
        level = self._level()
        if write:
            return level >= WRITE
        return level >= READ or self._anonymous_ok()

    def _from_this_pc(self) -> bool:
        return self.server_api.loopback_only or self.client_address[0] in ("127.0.0.1", "::1")

    def _refuse(self, message: str = "unauthorized", status: int = 401) -> None:
        """Answer a request that is not allowed."""
        self._discard_body()
        api = self.server_api
        if self._level() == THROTTLED:
            wait = max(1, int(api.throttle.blocked(self.client_address[0]) + 0.999))
            self._json(429, {"error": f"too many wrong tokens; try again in {wait} s"}, {"Retry-After": str(wait)})
        elif status == 401 and self._level() == NONE and not self._token() and self._from_this_pc() \
                and api.open_local_reads and not api.host_allowed(self.headers.get("Host")):
            self._json(403, {"error": "this host name is not known as this PC; use its IP address, or a token"})
        else:
            self._json(status, {"error": message})

    def _discard_body(self) -> None:
        """Take a small body of a refused request off the line, so that the answer arrives and the
        connection can be used again; after anything else the connection is closed."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length == 0:
            return
        if 0 < length <= 64 * 1024:
            self._reader.deadline = time.monotonic() + HEAD_TIMEOUT
            try:
                if len(self.rfile.read(length)) == length:
                    return
            except OSError:
                pass
            finally:
                self._reader.deadline = None
        self.close_connection = True

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)   # ValueError: answered with 400
        if length <= 0:
            return b""
        if length > MAX_BODY:
            self.close_connection = True
            raise ValueError("body too large")
        self._reader.deadline = time.monotonic() + BODY_TIMEOUT
        try:
            raw = self.rfile.read(length)
        finally:
            self._reader.deadline = None
        if len(raw) < length:
            self.close_connection = True
            raise ValueError("incomplete body")
        return raw

    def _body_json(self) -> Any:
        raw = self._body()
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}

    def _body_text(self) -> str:
        return self._body().decode("utf-8")

    # -- routes -------------------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        api = self.server_api
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/ws":
                self._websocket()
                return
            if path == "/":
                # The page itself holds no data and no secret: it asks for the token and sends it
                # with every request.  So it is served to whoever can reach the port, also from
                # the LAN without a token (where everything under /api then needs one).
                self._page()
                return
            if path == "/api/auth":
                level = self._level()
                if level < READ:
                    self._refuse("token required")
                    return
                self._json(200, {"ok": True, "writable": level >= WRITE, "admin": level >= ADMIN,
                                 "config_edit": api.config_refusal(level) is None})
                return
            if not self._authorized(write=False):
                self._refuse()
                return
            if path == "/api/health":
                self._json(200, api.backend.api_health())
            elif path == "/api/status":
                self._json(200, api.backend.api_status())
            elif path == "/api/users":
                self._json(200, api.backend.api_users())
            elif path.startswith("/api/users/") and path.endswith("/screenshot"):
                if not self._authorized(write=True):  # images are sensitive: always need the token
                    self._refuse("token required")
                    return
                user = unquote(path[len("/api/users/"):-len("/screenshot")])
                shot = api.backend.api_screenshot(user)
                if shot is None:
                    self._json(404, {"error": "no screenshot"})
                else:
                    data, fmt = shot
                    self._bytes(200, data, "image/png" if fmt == "png" else "image/jpeg")
            elif path.startswith("/api/users/") and path.endswith("/report"):
                query = parse_qs(urlsplit(self.path).query)
                try:
                    hours = float(query["hours"][0]) if "hours" in query else None
                except ValueError:
                    hours = None
                data = api.backend.api_report(unquote(path[len("/api/users/"):-len("/report")]), hours)
                self._json(200 if data else 404, data or {"error": "unknown user"})
            elif path.startswith("/api/users/"):
                user = unquote(path[len("/api/users/"):])
                data = api.backend.api_user(user)
                self._json(200 if data else 404, data or {"error": "unknown user"})
            elif path == "/api/entities":
                self._json(200, api.backend.api_entities())
            elif path == "/api/events":
                query = parse_qs(urlsplit(self.path).query)
                try:
                    n = int(query.get("n", ["50"])[0])
                except ValueError:
                    n = 50
                user = query.get("user", [None])[0]
                # since=<ts>: only what is newer; announced=1: the events that were pushed to Home
                # Assistant, in that shape (what a client asks for after it was disconnected)
                try:
                    since = float(query["since"][0]) if "since" in query else None
                except ValueError:
                    since = None
                if since is not None and since != since:   # NaN
                    since = None
                announced = query.get("announced", ["0"])[0].strip().lower() in ("1", "true", "yes", "on")
                extra: dict[str, Any] = {}
                if since is not None:
                    extra["since"] = since
                if announced:
                    extra["announced"] = True
                self._json(200, api.backend.api_events(min(max(n, 1), 500), user, **extra))
            elif path == "/api/config":
                if not self._config_allowed():
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

    def _config_allowed(self) -> bool:
        """Answer and return False unless this request may read / write the configuration."""
        level = self._level()
        if level < WRITE:
            self._refuse("token required")
            return False
        why = self.server_api.config_refusal(level)
        if why:
            self._refuse(why, 403)
            return False
        return True

    def do_POST(self):  # noqa: N802
        api = self.server_api
        path = urlsplit(self.path).path.rstrip("/")
        try:
            if not self._authorized(write=True):
                self._refuse("token required")
                return
            if path == "/api/command":
                data = self._body_json()
                if not isinstance(data, dict):
                    self._json(400, {"error": "expected a JSON object"})
                    return
                if is_admin_command(data) and self._level() < ADMIN:
                    self._json(403, {"ok": False, "error": "admin token required (api.admin_token)"})
                    return
                self._json(200, api.backend.api_command(data))
            elif path == "/api/reload":
                if self._level() < ADMIN:
                    self._refuse("admin token required (api.admin_token)", 403)
                    return
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
                self._refuse("token required")
                return
            if path == "/api/config":
                if not self._config_allowed():
                    return
                result = api.backend.api_config_write(self._body_text())
                self._json(200 if result.get("ok") else 400, result)
            else:
                self._json(404, {"error": "not found"})
        except ValueError as exc:   # a body that is too large, cut short or not UTF-8
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("api PUT %s failed", path)
            self._json(500, {"error": str(exc)})

    # -- websocket -----------------------------------------------------------------------------
    def _websocket(self) -> None:
        api = self.server_api
        if not self._authorized(write=False):
            self._refuse()
            return
        if not self._same_origin():
            self._json(403, {"error": "cross-origin WebSocket refused"})
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self._json(400, {"error": "websocket upgrade expected"})
            return
        level = self._level()
        address = self.client_address[0]
        accept = base64.b64encode(hashlib.sha1(key.encode("ascii", "replace") + _WS_MAGIC).digest()).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = self.connection
        # TLS: one connection object cannot be read and written at the same moment
        lock = threading.Lock() if isinstance(sock, ssl.SSLSocket) else None
        if lock is None:
            sock.settimeout(None)
        client = _WsClient(sock, lock=lock, anonymous=level < READ)
        self._reader.to_websocket(lambda: client.alive, lock)
        if not api._add_client(client):
            client.send(0x8, struct.pack("!H", 1013))  # try again later
            time.sleep(0.2)
            client.close()
            return
        reader = _WsReader(self.rfile)
        try:
            client.send_text(json.dumps({"type": "hello", "writable": level >= WRITE, "admin": level >= ADMIN,
                                         "status": api.backend.api_health()}, default=str))
            while client.alive:
                try:
                    frame = reader.read()
                except _WsProtocolError as exc:
                    log.debug("api: WebSocket client broke the protocol: %s", exc)
                    client.send(0x8, struct.pack("!H", exc.code))
                    time.sleep(0.2)   # let the close frame leave before the connection goes
                    break
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
                try:
                    if kind == "ping":
                        client.send_text(json.dumps({"type": "pong", "id": req_id}))
                    elif kind == "status":
                        client.send_text(json.dumps({"type": "status", "id": req_id, "pc": api.backend.api_status(),
                                                     "users": api.backend.api_users()}, default=str))
                    elif kind == "entities":
                        client.send_text(json.dumps({"type": "entities", "id": req_id, **api.backend.api_entities()}, default=str))
                    elif kind == "command":
                        client.send_text(json.dumps({"type": "result", "id": req_id,
                                                     **self._ws_command(msg, level, address)}, default=str))
                    else:
                        client.send_text(json.dumps({"type": "error", "id": req_id, "error": f"unknown type {kind}"}))
                except Exception as exc:  # noqa: BLE001 - one request that fails is answered, the connection stays
                    log.exception("api WebSocket %s request failed", kind)
                    client.send_text(json.dumps({"type": "result" if kind == "command" else "error", "id": req_id,
                                                 "ok": False, "error": str(exc)}, default=str))
        except OSError:
            pass
        finally:
            api._remove_client(client)
            client.close()

    def _ws_command(self, msg: dict[str, Any], level: int, address: str) -> dict[str, Any]:
        """Run one command from a WebSocket message; a ``token`` in it may raise the connection's level for it."""
        api = self.server_api
        if msg.get("token"):
            own = api.check(str(msg["token"]), address)
            if own == THROTTLED:
                wait = max(1, int(api.throttle.blocked(address) + 0.999))
                return {"ok": False, "error": f"too many wrong tokens; try again in {wait} s", "retry_after": wait}
            level = max(level, own)
        if level < WRITE:
            return {"ok": False, "error": "token required"}
        payload = {k: v for k, v in msg.items() if k not in ("type", "id", "token")}
        if is_admin_command(payload) and level < ADMIN:
            return {"ok": False, "error": "admin token required (api.admin_token)"}
        return api.backend.api_command(payload)


def read_config_text(path: Optional[str]) -> str:
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()
