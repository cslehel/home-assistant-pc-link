"""Keeps one PC's state and entity catalog up to date.

State is pushed by the PC over the local API's WebSocket every few seconds; a
slow REST poll takes over while the socket is down.  The entity catalog
(``GET /api/entities``) is fetched at start and refreshed when the PC reloads
its configuration or a user we have no entities for shows up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any, Optional

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PclinkApiError, PclinkAuthError, PclinkClient, PclinkOutdatedError
from .const import CATALOG_REFRESH_MIN_SECONDS, DOMAIN, EVENT_TYPE, FALLBACK_POLL_SECONDS, MANUFACTURER, STALE_PUSH_SECONDS

_LOGGER = logging.getLogger(__name__)

# Events after which the set of entities may have changed.
CATALOG_EVENTS = {"started", "config_reloaded", "logged_in"}


class PclinkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One instance per configured PC."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PclinkClient) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {entry.title}",
                         update_interval=timedelta(seconds=FALLBACK_POLL_SECONDS))
        self.entry = entry
        self.client = client
        self.health: dict[str, Any] = {}
        self.catalog: dict[str, Any] = {"device": {}, "users": {}, "entities": [], "triggers": {}}
        self.pc: dict[str, Any] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.known_keys: set[str] = set()
        self.ws_connected = False
        self.device_id: Optional[str] = None       # Home Assistant device registry id
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._stop = asyncio.Event()
        self._backoff = 1
        self._last_catalog_refresh = 0.0
        self._last_push = 0.0
        self._catalog_task: Optional[asyncio.Task] = None

    # -- identity -------------------------------------------------------------------------
    @property
    def pc_id(self) -> str:
        return str(self.health.get("device") or self.entry.unique_id or self.entry.entry_id)

    @property
    def pc_name(self) -> str:
        return str((self.catalog.get("device") or {}).get("name") or self.health.get("name") or self.entry.title)

    @property
    def signal_new_entities(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_new_entities"

    @property
    def online(self) -> bool:
        return self.ws_connected or self.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        dev = self.catalog.get("device") or {}
        info = DeviceInfo(
            identifiers={(DOMAIN, self.pc_id)},
            name=self.pc_name,
            manufacturer=str(dev.get("manufacturer") or MANUFACTURER),
            model=str(dev.get("model") or "Windows PC"),
            sw_version=str(dev.get("sw_version") or self.health.get("version") or ""),
            configuration_url=self.client.base_url,
        )
        if dev.get("suggested_area"):
            info["suggested_area"] = str(dev["suggested_area"])
        return info

    # -- lifecycle ------------------------------------------------------------------------
    async def async_setup(self) -> None:
        """Initial fetch; raises the config-entry exceptions Home Assistant expects."""
        try:
            self.health = await self.client.health()
            catalog = await self.client.entities()
            self.pc = await self.client.status()
            self.users = await self.client.users()
        except PclinkAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except PclinkOutdatedError as exc:
            raise ConfigEntryNotReady(f"{self.client.host}: {exc}") from exc
        except PclinkApiError as exc:
            raise ConfigEntryNotReady(f"{self.client.host}: {exc}") from exc
        self._apply_catalog(catalog, notify=False)
        self.async_set_updated_data({"pc": self.pc, "users": self.users})
        registry = dr.async_get(self.hass)
        device = registry.async_get_or_create(config_entry_id=self.entry.entry_id, **self.device_info)
        self.device_id = device.id
        self.entry.async_create_background_task(self.hass, self._ws_loop(), name=f"{DOMAIN} websocket {self.pc_id}")

    async def async_shutdown(self) -> None:
        self._stop.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._catalog_task is not None:
            self._catalog_task.cancel()
        await super().async_shutdown()

    # -- REST fallback --------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        if self.ws_connected and time.monotonic() - self._last_push > STALE_PUSH_SECONDS:
            # The socket looks open but the PC stopped pushing: treat it as gone,
            # close it so the WebSocket loop reconnects, and poll for the truth.
            _LOGGER.warning("No state from %s for %ds; reconnecting", self.pc_name, STALE_PUSH_SECONDS)
            self.ws_connected = False
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
        if self.ws_connected:  # pushed data is authoritative while the socket is up
            return {"pc": self.pc, "users": self.users}
        try:
            self.pc = await self.client.status()
            self.users = await self.client.users()
        except PclinkAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except PclinkApiError as exc:
            raise UpdateFailed(str(exc)) from exc
        self._check_unknown_users()
        return {"pc": self.pc, "users": self.users}

    # -- WebSocket ------------------------------------------------------------------------
    async def _ws_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ws = await self.client.ws_connect()
            except PclinkAuthError:
                _LOGGER.error("%s rejected the API token; re-authentication required", self.pc_name)
                self.entry.async_start_reauth(self.hass)
                return
            except PclinkApiError as exc:
                _LOGGER.debug("websocket connect to %s failed: %s", self.pc_name, exc)
                await self._pause()
                continue
            self._ws = ws
            try:
                await self.client.ws_request(ws, "status", 1)
                await self.client.ws_request(ws, "entities", 2)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except ValueError:
                            continue
                        if isinstance(data, dict):
                            self._handle_message(data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                _LOGGER.debug("websocket to %s dropped: %s", self.pc_name, exc)
            finally:
                self._ws = None
                if not ws.closed:
                    await ws.close()
                if self.ws_connected:
                    self.ws_connected = False
                    if not self._stop.is_set():
                        _LOGGER.warning("Lost the connection to %s; polling until it is back", self.pc_name)
                        self.async_update_listeners()
                        await self.async_request_refresh()  # notice an offline PC right away
            if not self._stop.is_set():
                await self._pause()

    async def _pause(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._backoff)
        except asyncio.TimeoutError:
            pass
        self._backoff = min(self._backoff * 2, 60)

    @callback
    def _handle_message(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind in ("hello", "state", "status"):
            self._last_push = time.monotonic()
        if kind == "hello":
            self._backoff = 1
            if isinstance(data.get("status"), dict):
                self.health = data["status"]
            if not self.ws_connected:
                self.ws_connected = True
                _LOGGER.debug("connected to %s", self.pc_name)
                self.async_update_listeners()
        elif kind in ("state", "status"):
            if isinstance(data.get("pc"), dict):
                self.pc = data["pc"]
            if isinstance(data.get("users"), dict):
                self.users = data["users"]
            self.ws_connected = True
            self.async_set_updated_data({"pc": self.pc, "users": self.users})
            self._check_unknown_users()
        elif kind == "entities":
            self._apply_catalog(data)
        elif kind == "event":
            self._handle_event(data)

    @callback
    def _handle_event(self, data: dict[str, Any]) -> None:
        payload = {k: v for k, v in data.items() if k != "type"}
        event = str(payload.get("event") or "")
        self.hass.bus.async_fire(EVENT_TYPE, {"device_id": self.device_id, "pc_id": self.pc_id,
                                              "pc_name": self.pc_name, "entry_id": self.entry.entry_id, **payload})
        if event in CATALOG_EVENTS:
            self._schedule_catalog_refresh()

    # -- catalog --------------------------------------------------------------------------
    @callback
    def _apply_catalog(self, cat: dict[str, Any], notify: bool = True) -> None:
        entities = [e for e in (cat.get("entities") or []) if isinstance(e, dict) and e.get("key") and e.get("platform")]
        self.catalog = {"device": cat.get("device") or {}, "users": cat.get("users") or {},
                        "entities": entities, "triggers": cat.get("triggers") or {}, "version": cat.get("version")}
        self._last_catalog_refresh = time.monotonic()
        new = [e for e in entities if e["key"] not in self.known_keys]
        self.known_keys.update(e["key"] for e in new)
        if new and notify:
            _LOGGER.debug("%s: %d new entities", self.pc_name, len(new))
            async_dispatcher_send(self.hass, self.signal_new_entities, new)
        if self.device_id:
            dr.async_get(self.hass).async_update_device(self.device_id, name=self.pc_name,
                                                        sw_version=self.device_info.get("sw_version"))

    def catalog_entities(self, platform: Optional[str] = None) -> list[dict[str, Any]]:
        return [e for e in self.catalog["entities"] if platform is None or e["platform"] == platform]

    @callback
    def _check_unknown_users(self) -> None:
        if any(user not in self.catalog["users"] for user in self.users):
            self._schedule_catalog_refresh()

    @callback
    def _schedule_catalog_refresh(self) -> None:
        if self._catalog_task is not None and not self._catalog_task.done():
            return
        self._catalog_task = self.entry.async_create_task(self.hass, self._refresh_catalog(), name=f"{DOMAIN} catalog {self.pc_id}")

    async def _refresh_catalog(self) -> None:
        wait = CATALOG_REFRESH_MIN_SECONDS - (time.monotonic() - self._last_catalog_refresh)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            self._apply_catalog(await self.client.entities())
        except PclinkApiError as exc:
            _LOGGER.debug("catalog refresh for %s failed: %s", self.pc_name, exc)

    # -- values ---------------------------------------------------------------------------
    def payload_for(self, spec: dict[str, Any]) -> Optional[dict[str, Any]]:
        if spec.get("source") == "user":
            return self.users.get(str(spec.get("user", "")))
        return self.pc

    def value(self, spec: dict[str, Any], field: str = "value") -> Any:
        path = spec.get(field)
        payload = self.payload_for(spec)
        if not path or payload is None:
            return None
        cur: Any = payload
        for part in str(path).split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
            if cur is None:
                return None
        return cur
