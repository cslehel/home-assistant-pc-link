"""Thin async client for the PC Link local API (REST + WebSocket)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


class PclinkApiError(Exception):
    """Base error."""


class PclinkConnectionError(PclinkApiError):
    """The PC could not be reached."""


class PclinkAuthError(PclinkApiError):
    """The token was rejected."""


class PclinkOutdatedError(PclinkApiError):
    """The service on the PC is too old for this integration (no entity catalog)."""


class PclinkCommandError(PclinkApiError):
    """The PC refused a command (e.g. parent PIN required)."""


class PclinkClient:
    """REST calls plus a WebSocket connection helper.  All errors are mapped to PclinkApiError."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: Optional[str]) -> None:
        self._session = session
        self.host = host
        self.port = int(port)
        self.token = token or ""

    # -- helpers --------------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"http://{host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return self.base_url.replace("http://", "ws://", 1) + "/api/ws"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request(self, method: str, path: str, *, json_body: Any = None, expect_bytes: bool = False) -> Any:
        url = self.base_url + path
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(method, url, headers=self._headers(), json=json_body) as resp:
                    if resp.status == 401:
                        raise PclinkAuthError("token rejected")
                    if resp.status == 404:
                        raise PclinkApiError(f"{path}: not found")
                    if resp.status >= 400:
                        text = await resp.text()
                        raise PclinkApiError(f"{path}: HTTP {resp.status} {text[:200]}")
                    if expect_bytes:
                        return await resp.read()
                    return await resp.json(content_type=None)
        except PclinkApiError:
            raise
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
        except PclinkApiError as exc:
            if isinstance(exc, (PclinkAuthError, PclinkConnectionError)):
                raise
            raise PclinkOutdatedError("the PC Link service does not provide /api/entities; update it") from exc
        if not isinstance(data, dict) or "entities" not in data:
            raise PclinkOutdatedError("unexpected entity catalog")
        return data

    async def events(self, n: int = 50, user: Optional[str] = None) -> list[dict[str, Any]]:
        query = f"?n={int(n)}" + (f"&user={user}" if user else "")
        return await self._request("GET", "/api/events" + query)

    async def send(self, data: dict[str, Any]) -> dict[str, Any]:
        """Run a JSON command; returns the PC's {ok, error, ...} result without raising on refusal."""
        result = await self._request("POST", "/api/command", json_body=data)
        return result if isinstance(result, dict) else {"ok": False, "error": "unexpected response"}

    async def command(self, data: dict[str, Any]) -> dict[str, Any]:
        """Run a JSON command and raise PclinkCommandError when the PC refuses it."""
        result = await self.send(data)
        if not result.get("ok", False):
            raise PclinkCommandError(str(result.get("error") or "refused"))
        return result

    async def set_entity(self, key: str, value: Any, pin: Optional[str] = None) -> dict[str, Any]:
        data: dict[str, Any] = {"action": "set", "entity": key, "value": value}
        if pin:
            data["pin"] = pin
        return await self.command(data)

    async def screenshot(self, user: str) -> Optional[bytes]:
        try:
            return await self._request("GET", f"/api/users/{user}/screenshot", expect_bytes=True)
        except PclinkApiError as exc:
            if isinstance(exc, (PclinkAuthError, PclinkConnectionError)):
                raise
            return None

    # -- WebSocket ------------------------------------------------------------------------
    async def ws_connect(self) -> aiohttp.ClientWebSocketResponse:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                return await self._session.ws_connect(self.ws_url, headers=self._headers(), heartbeat=30, max_msg_size=16 * 1024 * 1024)
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status == 401:
                raise PclinkAuthError("token rejected") from exc
            raise PclinkConnectionError(f"websocket handshake failed: {exc}") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise PclinkConnectionError(f"cannot reach {self.ws_url}: {exc}") from exc

    @staticmethod
    async def ws_request(ws: aiohttp.ClientWebSocketResponse, kind: str, req_id: int, **fields: Any) -> None:
        await ws.send_str(json.dumps({"type": kind, "id": req_id, **fields}))
