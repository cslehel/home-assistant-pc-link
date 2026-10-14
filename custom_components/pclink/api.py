"""Thin async client for the PC Link local API (REST + WebSocket)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
WS_CLOSE_TIMEOUT = 5

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class PclinkApiError(Exception):
    """Base error."""


class PclinkConnectionError(PclinkApiError):
    """The PC could not be reached."""


class PclinkTlsError(PclinkConnectionError):
    """The PC answered, but its certificate is not the expected one (or it does not speak HTTPS)."""


class PclinkAuthError(PclinkApiError):
    """The token was rejected."""


class PclinkThrottledError(PclinkApiError):
    """The PC refuses this address for a while after too many wrong tokens (HTTP 429), or
    refused a command with a ``retry_after`` in its JSON answer.  ``retry_after`` is how many
    seconds it asked us to wait (its Retry-After header or that field).  The coordinator
    raises it too - without sending anything - while that time is not up."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PclinkBusyError(PclinkThrottledError):
    """The PC has no connection free for us right now (HTTP 503, with Retry-After); nothing
    is wrong with the token or the address."""


def retry_after(headers: Any, default: float = 60.0) -> float:
    """Seconds from a Retry-After header (a number; a date or nothing gives ``default``), 1 s to 1 h."""
    try:
        value = float(str((headers or {}).get("Retry-After", "")).strip())
    except (TypeError, ValueError, AttributeError):
        value = default
    if not value == value:  # NaN
        value = default
    return min(max(value, 1.0), 3600.0)


def reply_retry_after(reply: Any) -> Optional[float]:
    """The ``retry_after`` of a refused JSON answer (``{"ok": false, "retry_after": N}``) in
    seconds, 1 s to 1 h; None when the answer has none (or one that is not a number)."""
    if not isinstance(reply, dict) or reply.get("ok") is not False:
        return None
    value = reply.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value == value:
        return None
    return min(max(float(value), 1.0), 3600.0)


class PclinkNotFoundError(PclinkApiError):
    """The PC answered 404 (unknown path, user or no screenshot yet)."""


class PclinkOutdatedError(PclinkApiError):
    """The service on the PC is too old for this integration (no entity catalog)."""


class PclinkCommandError(PclinkApiError):
    """The PC refused a command (e.g. parent PIN required)."""


class PclinkAdminRequiredError(PclinkCommandError):
    """The command is one only the PC's admin token may send (``api.admin_token``); the token
    itself is fine for everything else, so this is no reason to ask for a new one."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("this needs the PC's admin token (api.admin_token); the token entered for this PC is its everyday token")
        self.detail = detail


def _refusal(text: str) -> str:
    """The ``error`` of a JSON refusal, or the text itself (shortened)."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    return str(data.get("error") or "refused") if isinstance(data, dict) else (text.strip()[:200] or "refused")


class PclinkWrongDeviceError(PclinkApiError):
    """A different PC answers at the configured address; nothing is sent to it."""


def user_path(user: str, suffix: str = "") -> str:
    """``/api/users/<user><suffix>`` with the user name percent-encoded (spaces, ``#``, ``/``, non-ASCII)."""
    return f"/api/users/{quote(str(user), safe='')}{suffix}"


def normalize_fingerprint(text: Any) -> Optional[str]:
    """A certificate's SHA-256 fingerprint as 64 lower-case hex digits (``AA:BB:..`` and blanks
    are accepted); None for an empty value.  Raises ValueError for anything else."""
    digits = "".join(ch for ch in str(text or "") if ch not in ": -\t").lower()
    if digits.startswith("sha256") and len(digits) > 64:
        digits = digits[6:].lstrip("=")
    if not digits:
        return None
    if not _FINGERPRINT_RE.match(digits):
        raise ValueError("a SHA-256 fingerprint has 64 hex digits")
    return digits


def split_address(text: str) -> tuple[str, Optional[int], Optional[bool]]:
    """What a user typed as the address -> ``(host, port or None, https or None)``.

    People paste what their browser shows: ``http://192.168.1.5:47392/``, ``pc.local:47392``
    or an IPv6 address in brackets.  A bare IPv6 address is left alone.  Raises ValueError
    when no host can be made out."""
    raw = str(text or "").strip()
    tls: Optional[bool] = None
    port: Optional[int] = None
    if "://" in raw:
        scheme = raw.split("://", 1)[0].lower()
        if scheme not in ("http", "https", "ws", "wss"):
            raise ValueError(f"unknown scheme {scheme}")
        tls = scheme in ("https", "wss")
        parts = urlsplit(raw)
        host, port = parts.hostname or "", parts.port
    else:
        raw = raw.split("/", 1)[0]
        if raw.startswith("["):  # [ipv6]:port
            host, _, rest = raw[1:].partition("]")
            if rest.startswith(":") and rest[1:].isdigit():
                port = int(rest[1:])
        elif raw.count(":") == 1:  # host:port
            host, _, tail = raw.partition(":")
            if not tail.isdigit():
                raise ValueError("the port must be a number")
            port = int(tail)
        else:  # a name, an IPv4 address or a bare IPv6 address
            host = raw
    host = host.strip()
    if not host or any(ch.isspace() for ch in host) or "@" in host:
        raise ValueError("no host name or address")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("the port must be between 1 and 65535")
    return host, port, tls


class PclinkClient:
    """REST calls plus a WebSocket connection helper.  All errors are mapped to PclinkApiError.

    ``tls`` switches to HTTPS / WSS (``api.tls_cert`` on the PC).  With a ``fingerprint``
    (SHA-256 of the PC's certificate, hex) exactly that certificate is accepted, which is what
    a self-signed one needs; without one the certificate is verified the usual way."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: Optional[str],
                 tls: bool = False, fingerprint: Optional[str] = None) -> None:
        self._session = session
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.fingerprint = normalize_fingerprint(fingerprint) if fingerprint else None
        self.tls = bool(tls or self.fingerprint)
        # passed to aiohttp only when set, so a plain-HTTP PC is asked exactly as before
        self._extra: dict[str, Any] = {"ssl": aiohttp.Fingerprint(bytes.fromhex(self.fingerprint))} if self.fingerprint else {}

    # -- helpers --------------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{'https' if self.tls else 'http'}://{host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return ("wss://" if self.tls else "ws://") + self.base_url.split("://", 1)[1] + "/api/ws"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request(self, method: str, path: str, *, json_body: Any = None, expect_bytes: bool = False) -> Any:
        url = self.base_url + path
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(method, url, headers=self._headers(), json=json_body, **self._extra) as resp:
                    if resp.status == 401:
                        raise PclinkAuthError("token rejected")
                    if resp.status == 404:
                        raise PclinkNotFoundError(f"{path}: not found")
                    if resp.status == 403:
                        # understood and refused: the token is valid but may not do this
                        reason = _refusal(await resp.text())
                        if "admin token" in reason.lower():
                            raise PclinkAdminRequiredError(reason)
                        raise PclinkCommandError(reason)
                    if resp.status == 429:
                        wait = retry_after(resp.headers)
                        raise PclinkThrottledError(f"{path}: too many wrong tokens; the PC asks to wait {int(wait)} s", wait)
                    if resp.status == 503 and "Retry-After" in resp.headers:
                        wait = retry_after(resp.headers, 5.0)
                        raise PclinkBusyError(f"{path}: the PC is busy; it asks to wait {int(wait)} s", wait)
                    if resp.status >= 400:
                        text = await resp.text()
                        raise PclinkApiError(f"{path}: HTTP {resp.status} {text[:200]}")
                    if expect_bytes:
                        return await resp.read()
                    return await resp.json(content_type=None)
        except PclinkApiError:
            raise
        except (aiohttp.ServerFingerprintMismatch, aiohttp.ClientSSLError) as exc:
            raise PclinkTlsError(f"{self.base_url}: the certificate was not accepted ({exc})") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise PclinkConnectionError(f"cannot reach {self.base_url}: {exc}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise PclinkApiError(f"{path}: invalid JSON ({exc})") from exc

    # -- REST -----------------------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        data = await self._request("GET", "/api/health")
        if not isinstance(data, dict) or not data.get("ok"):
            raise PclinkApiError("unexpected health response")
        return data

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")

    async def users(self) -> dict[str, dict[str, Any]]:
        return await self._request("GET", "/api/users")

    async def entities(self) -> dict[str, Any]:
        """The entity catalog; raises PclinkOutdatedError when the PC runs an old service."""
        try:
            data = await self._request("GET", "/api/entities")
        except PclinkNotFoundError as exc:  # only a missing endpoint means "too old"; a 500 stays a plain error
            raise PclinkOutdatedError("the PC Link service does not provide /api/entities; update it") from exc
        if not isinstance(data, dict) or "entities" not in data:
            raise PclinkOutdatedError("unexpected entity catalog")
        return data

    async def check_write_auth(self) -> dict[str, Any]:
        """Prove the token is accepted for writes, without side effects.

        Current services answer ``GET /api/auth`` with what the token may do
        (``{"ok", "writable", "admin"}``).  An older one has no such endpoint
        (404); there ``GET /api/config`` - which always needs the token, while
        plain reads do not when Home Assistant reaches the PC over loopback - is
        used purely as a probe and its body is discarded.  Raises PclinkAuthError
        when the token is rejected or only valid for reading; any other refusal
        of the old probe (unreadable file, editing switched off) says nothing
        about the token and is ignored.  Returns what is known: ``writable`` and,
        from a current service, ``admin``."""
        try:
            answer = await self._request("GET", "/api/auth")
        except PclinkNotFoundError:
            answer = None
        if isinstance(answer, dict):
            if answer.get("writable") is False:
                raise PclinkAuthError("the token is only valid for reading")
            # the everyday token and the admin token are both fine; ``admin`` says which it is
            return {"writable": True, "admin": bool(answer.get("admin")), "config_edit": bool(answer.get("config_edit"))}
        try:
            await self._request("GET", "/api/config", expect_bytes=True)
        except (PclinkAuthError, PclinkConnectionError):
            raise
        except PclinkApiError as exc:
            _LOGGER.debug("auth probe inconclusive: %s", exc)
        return {"writable": True}

    async def events(self, n: int = 50, user: Optional[str] = None, since: Optional[float] = None,
                     announced: bool = False) -> list[dict[str, Any]]:
        """Recent events, newest first.  ``announced`` asks for the events that are also pushed
        over the WebSocket, in that shape, and ``since`` for those after a time stamp; a
        service that knows neither ignores them and answers with its event log (``kind``
        where pushed events say ``event``), so the caller has to cope with both."""
        query = f"?n={int(n)}" + (f"&user={quote(str(user), safe='')}" if user else "")
        if since is not None:
            query += f"&since={float(since)!r}"
        if announced:
            query += "&announced=1"
        data = await self._request("GET", "/api/events" + query)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    async def send(self, data: dict[str, Any]) -> dict[str, Any]:
        """Run a JSON command; returns the PC's {ok, error, ...} result without raising on refusal."""
        result = await self._request("POST", "/api/command", json_body=data)
        return result if isinstance(result, dict) else {"ok": False, "error": "unexpected response"}

    async def command(self, data: dict[str, Any]) -> dict[str, Any]:
        """Run a JSON command and raise PclinkCommandError when the PC refuses it -
        PclinkThrottledError when the refusal says how long to wait (``retry_after``)."""
        result = await self.send(data)
        if not result.get("ok", False):
            reason = str(result.get("error") or "refused")
            wait = reply_retry_after(result)
            if wait is not None:
                raise PclinkThrottledError(reason, wait)
            if "admin token" in reason.lower():
                raise PclinkAdminRequiredError(reason)
            raise PclinkCommandError(reason)
        return result

    async def set_entity(self, key: str, value: Any, pin: Optional[str] = None) -> dict[str, Any]:
        data: dict[str, Any] = {"action": "set", "entity": key, "value": value}
        if pin:
            data["pin"] = pin
        return await self.command(data)

    async def screenshot(self, user: str) -> Optional[bytes]:
        try:
            return await self._request("GET", user_path(user, "/screenshot"), expect_bytes=True)
        except PclinkNotFoundError:
            return None

    # -- WebSocket ------------------------------------------------------------------------
    async def ws_connect(self) -> aiohttp.ClientWebSocketResponse:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                return await self._session.ws_connect(self.ws_url, headers=self._headers(), heartbeat=30,
                                                      max_msg_size=16 * 1024 * 1024, **self._extra)
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status == 401:
                raise PclinkAuthError("token rejected") from exc
            if exc.status == 429:
                wait = retry_after(getattr(exc, "headers", None))
                raise PclinkThrottledError(f"websocket refused: too many wrong tokens; the PC asks to wait {int(wait)} s", wait) from exc
            if exc.status == 503:
                wait = retry_after(getattr(exc, "headers", None), 5.0)
                raise PclinkBusyError(f"websocket refused: the PC is busy; it asks to wait {int(wait)} s", wait) from exc
            raise PclinkConnectionError(f"websocket handshake failed: {exc}") from exc
        except (aiohttp.ServerFingerprintMismatch, aiohttp.ClientSSLError) as exc:
            raise PclinkTlsError(f"{self.ws_url}: the certificate was not accepted ({exc})") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise PclinkConnectionError(f"cannot reach {self.ws_url}: {exc}") from exc

    @staticmethod
    async def ws_request(ws: aiohttp.ClientWebSocketResponse, kind: str, req_id: int, **fields: Any) -> None:
        await ws.send_str(json.dumps({"type": kind, "id": req_id, **fields}))
