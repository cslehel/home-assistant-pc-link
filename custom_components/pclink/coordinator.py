"""Keeps one PC's state and entity catalog up to date.

State is pushed by the PC over the local API's WebSocket every few seconds; a
slow REST poll takes over while the socket is down.  The entity catalog
(``GET /api/entities``) is fetched at start and refreshed when the PC says it
changed (``catalog_hash`` in its state), reloads its configuration or a user
we have no entities for shows up.

Whatever answers at the configured address has to be the PC the entry was
made for (``device`` in its health document against the entry's unique id);
a different PC is never listened to, never sent a command and never a reason
to remove anything.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import random
import time
from collections import deque
from datetime import timedelta
from typing import Any, Optional

import aiohttp
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    WS_CLOSE_TIMEOUT,
    PclinkApiError,
    PclinkAuthError,
    PclinkBusyError,
    PclinkClient,
    PclinkOutdatedError,
    PclinkThrottledError,
    PclinkWrongDeviceError,
    reply_retry_after,
)
from .const import (
    CATALOG_REFRESH_MAX_SECONDS,
    CATALOG_REFRESH_MIN_SECONDS,
    CATALOG_REMOVE_GRACE_SECONDS,
    DOMAIN,
    EVENT_REPLAY_MAX,
    EVENT_REPLAY_MAX_AGE_SECONDS,
    EVENT_SAME_SECONDS,
    EVENT_TYPE,
    FALLBACK_POLL_SECONDS,
    ISSUE_MOVED,
    ISSUE_WRONG_PC,
    MANUFACTURER,
    PC_TRIGGERS,
    ROLLUP_UNIQUE_PREFIX,
    STALE_PUSH_INTERVALS,
    STALE_PUSH_MAX_SECONDS,
    STALE_PUSH_SECONDS,
    USAGE_CACHE_SECONDS,
    USER_TRIGGERS,
    WS_HEALTHY_SECONDS,
    issue_id,
)

_LOGGER = logging.getLogger(__name__)

# Events after which the set of entities may have changed.
CATALOG_EVENTS = {"started", "config_reloaded", "logged_in", "rules_changed"}

# What is kept of every user's payload across restarts: enough for the roll-up's minutes
# and for the shared budgets of the other PCs while this PC is switched off.
_USAGE_KEYS = ("name", "date", "used_minutes", "gaming_minutes", "browsing_minutes", "history",
               "bonus_local_minutes", "bedtime_extension_local_minutes")
_EVENTS_REMEMBERED = 200   # recent events, to tell a replayed one from one that arrived live
_EVENTS_STORED = 50


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def usage_summary(users: Any) -> dict[str, dict[str, Any]]:
    """The part of the users' payloads that is stored (see ``_USAGE_KEYS``)."""
    out: dict[str, dict[str, Any]] = {}
    for user, payload in (users.items() if isinstance(users, dict) else []):
        if isinstance(payload, dict) and isinstance(payload.get("date"), str):
            out[str(user)] = {key: payload[key] for key in _USAGE_KEYS if key in payload}
    return out


class PclinkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One instance per configured PC."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PclinkClient) -> None:
        # Current Home Assistant wants the config entry passed explicitly; releases before
        # 2024.11 do not know the argument.
        extra: dict[str, Any] = {}
        if "config_entry" in inspect.signature(DataUpdateCoordinator.__init__).parameters:
            extra["config_entry"] = entry
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {entry.title}",
                         update_interval=timedelta(seconds=FALLBACK_POLL_SECONDS), **extra)
        self.entry = entry
        self.client = client
        self.health: dict[str, Any] = {}
        self.catalog: dict[str, Any] = {"device": {}, "users": {}, "entities": [], "triggers": {}}
        self.pc: dict[str, Any] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.catalog_keys: set[str] = set()         # keys in the current catalog
        self.live: dict[str, Any] = {}              # key -> entity object created for it
        self.ws_connected = False                   # True from the first state pushed over the socket on
        self._hello_seen = False                    # the open socket's hello arrived
        self._ws_requests_after = 0.0               # monotonic time before which the PC asked not to be asked over the socket
        self._hold_until = 0.0                      # ... nor sent commands or polled (its retry_after / Retry-After)
        self.connections = 0                       # WebSocket connections so far (a new one may be a restarted PC)
        self.device_id: Optional[str] = None       # Home Assistant device registry id
        # {"device", "name"} of the PC that answers instead of ours; None while all is well
        self.wrong_pc: Optional[dict[str, str]] = None
        self._identity_confirmed = False            # the PC proved who it is since setup / the last connection
        # May our token do what only the admin token may (run custom commands, reload the
        # configuration, install an update)?  None until the PC has said (an older one never does).
        self.admin: Optional[bool] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_rejected = False                   # the socket's hello came from a different PC
        self._request_id = 10
        self._stop = asyncio.Event()
        self._backoff = 1
        self._last_catalog_refresh = 0.0
        self._catalog_min_wait = float(CATALOG_REFRESH_MIN_SECONDS)
        self._catalog_again = False                 # a refresh was asked for while one was running
        self._catalog_tried: Optional[str] = None   # the state's catalog signature a refresh was last started for
        self._absent_users: set[str] = set()        # in the state but (as last checked) not in the catalog
        self._missing: dict[str, float] = {}        # registry unique_id -> when the catalog first omitted it
        self._unsub_recheck: Optional[CALLBACK_TYPE] = None
        self._unsub_stop: Optional[CALLBACK_TYPE] = None
        self._last_push = 0.0
        self._last_state_push = 0.0
        self._push_interval = 0.0                   # measured (or announced) seconds between state pushes
        self._stale_after = float(STALE_PUSH_SECONDS)
        self._stale_warned = False
        self._await_poll = False                    # the socket just dropped; the poll has not confirmed the PC yet
        self._reauth_logged = False
        self._logged_message_errors: set[str] = set()
        self._catalog_task: Optional[asyncio.Task] = None
        self._replay_task: Optional[asyncio.Task] = None
        # Events: the newest time stamp seen (None until the first connection ever) and the
        # recent ones, so that what the PC recorded while we were away is fired exactly once.
        self.last_event_ts: Optional[float] = None
        self._events_seen: deque[tuple[float, str, str]] = deque(maxlen=_EVENTS_REMEMBERED)
        # What the PC last told us, kept across restarts: with it the entities (and the
        # Wake-on-LAN button, which needs the MAC address) exist while the PC is switched off,
        # and its users' minutes of today still count in the roll-up.
        self._cache_store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._last_usage_save = 0.0
        self._usage_saved_dates: tuple[str, ...] = ()
        self.wake_mac: Optional[str] = None
        self.wake_ip: Optional[str] = None
        self.wake_broadcast: Optional[str] = None

    # -- identity -------------------------------------------------------------------------
    @property
    def pc_id(self) -> str:
        """The PC's ``device.id``.  The entry's unique id is that id as it was when the PC
        was added, and what answers has to match it, so it is the one that counts."""
        return str(self.entry.unique_id or self.health.get("device") or self.entry.entry_id)

    @property
    def pc_name(self) -> str:
        return str((self.catalog.get("device") or {}).get("name") or self.health.get("name") or self.entry.title)

    @property
    def signal_new_entities(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_new_entities"

    @property
    def online(self) -> bool:
        if self.wrong_pc is not None:
            return False
        return self.ws_connected or (self.last_update_success and not self._await_poll)

    @property
    def wake_unique_id(self) -> str:
        return f"pclink_{self.pc_id}_wake_on_lan"

    def unique_id_for(self, spec: dict[str, Any]) -> str:
        return str(spec.get("unique_id") or f"pclink_{self.pc_id}_{spec['key']}")

    @callback
    def async_start_reauth(self, reason: str) -> None:
        """Ask the user for a new token.  Home Assistant keeps one such request open at a
        time; when it was dismissed, the next rejected call brings it back."""
        if not self._reauth_logged:
            self._reauth_logged = True
            _LOGGER.error("%s: %s; re-authentication required", self.pc_name, reason)
        self.entry.async_start_reauth(self.hass)

    @callback
    def _check_identity(self, health: Any) -> bool:
        """Is what answered the PC this entry was made for?  Raises / clears the repair issue."""
        expected = self.entry.unique_id
        found = health.get("device") if isinstance(health, dict) else None
        if not expected or found in (None, "") or str(found) == str(expected):
            if self.wrong_pc is not None:
                _LOGGER.info("%s answers at %s again", self.entry.title, self.client.base_url)
                self.wrong_pc = None
                self._identity_confirmed = False
            if not self._identity_confirmed:
                # The first good answer since setup (or since a different PC answered): an issue
                # left from before - also one fixed by "Reconfigure", which reloads the entry with
                # wrong_pc unset - is moot now.
                self._identity_confirmed = True
                ir.async_delete_issue(self.hass, DOMAIN, issue_id(ISSUE_WRONG_PC, self.entry.entry_id))
            return True
        self._identity_confirmed = False
        wrong = {"device": str(found), "name": str(health.get("name") or found)}
        if wrong != self.wrong_pc:
            _LOGGER.error("%s is configured at %s, but %s (%s) answers there; it is ignored. "
                          "Reconfigure the entry with the PC's current address",
                          self.entry.title, self.client.base_url, wrong["name"], wrong["device"])
            self.wrong_pc = wrong
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id(ISSUE_WRONG_PC, self.entry.entry_id), is_fixable=True,
                severity=ir.IssueSeverity.ERROR, translation_key=ISSUE_WRONG_PC,
                translation_placeholders={"name": self.entry.title, "expected": str(expected), "found": wrong["device"],
                                          "found_name": wrong["name"], "address": f"{self.client.host}:{self.client.port}"},
                data={"entry_id": self.entry.entry_id, "host": self.client.host, "port": self.client.port})
            self.ws_connected = False
            self.async_update_listeners()
        self._hello_seen = False  # its socket is hung up on: no "lost the connection" for it
        return False

    def _refuse_wrong_pc(self) -> None:
        if self.wrong_pc is not None:
            raise PclinkWrongDeviceError(
                f"a different PC ({self.wrong_pc['name']}) answers at {self.client.host}; nothing was sent to it")

    @property
    def device_info(self) -> DeviceInfo:
        dev = self.catalog.get("device") or {}
        info = DeviceInfo(
            identifiers={(DOMAIN, self.pc_id)},
            name=self.pc_name,
            manufacturer=str(dev.get("manufacturer") or MANUFACTURER),
            model=str(dev.get("model") or "Windows PC"),
            sw_version=str(dev.get("sw_version") or self.health.get("version") or ""),
            # the PC's setup / status page: it holds no data and asks for the token itself
            configuration_url=self.client.base_url,
        )
        if dev.get("suggested_area"):
            info["suggested_area"] = str(dev["suggested_area"])
        return info

    # -- lifecycle ------------------------------------------------------------------------
    async def async_setup(self) -> None:
        """Initial fetch; raises the config-entry exceptions Home Assistant expects."""
        cached = await self._load_cache()
        if cached is not None:
            self.last_event_ts = cached["last_event_ts"]
            self._events_seen.extend(cached["events_seen"])
        try:
            health = await self.client.health()
            if not self._check_identity(health):
                raise PclinkWrongDeviceError(f"{self.wrong_pc['name'] if self.wrong_pc else 'another PC'} answers instead")
            catalog = await self.client.entities()
            pc = await self.client.status()
            users = await self.client.users()
        except PclinkAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except PclinkOutdatedError as exc:
            raise ConfigEntryNotReady(f"{self.client.host}: {exc}") from exc
        except PclinkApiError as exc:
            if cached is None or cached["catalog"] is None:
                raise ConfigEntryNotReady(f"{self.client.host}: {exc}") from exc
            # The PC is off or unreachable but known: start from what it told us last time.
            _LOGGER.info("%s is not reachable (%s); starting with its last known entities", self.entry.title, exc)
            self.health = cached["health"]
            self.users = cached["users"]
            self.wake_mac, self.wake_ip, self.wake_broadcast = cached["mac"], cached["ip"], cached["broadcast"]
            self._apply_catalog(cached["catalog"], notify=False)
            self.async_set_update_error(exc)
        else:
            self.health, self.pc, self.users = health, pc, users if isinstance(users, dict) else {}
            if cached is not None:  # kept when the PC does not report an address this time
                self.wake_mac, self.wake_ip, self.wake_broadcast = cached["mac"], cached["ip"], cached["broadcast"]
            self._apply_catalog(catalog, notify=False)
            self._remember_addresses()
            self._cache_store.async_delay_save(self._cache_data, 10)
            self.async_set_updated_data({"pc": self.pc, "users": self.users})
        registry = dr.async_get(self.hass)
        device = registry.async_get_or_create(config_entry_id=self.entry.entry_id, **self.device_info)
        self.device_id = device.id
        self._unsub_stop = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._save_on_stop)
        self.entry.async_create_background_task(self.hass, self._ws_loop(), name=f"{DOMAIN} websocket {self.pc_id}")

    async def _load_cache(self) -> Optional[dict[str, Any]]:
        """The stored cache with every part checked for its type; None when there is none.
        A damaged or foreign file is treated as "nothing stored", never as an error."""
        try:
            raw = await self._cache_store.async_load()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("%s: the stored state could not be read (%s); starting without it", self.entry.title, exc)
            return None
        if not isinstance(raw, dict):
            return None

        def text(key: str) -> Optional[str]:
            value = raw.get(key)
            return value if isinstance(value, str) and value else None

        seen: list[tuple[float, str, str]] = []
        for item in raw.get("events_seen") if isinstance(raw.get("events_seen"), list) else []:
            if isinstance(item, (list, tuple)) and len(item) == 3 and _number(item[0]) is not None:
                seen.append((float(item[0]), str(item[1]), str(item[2])))
        return {
            "health": raw["health"] if isinstance(raw.get("health"), dict) else {},
            "catalog": raw["catalog"] if isinstance(raw.get("catalog"), dict) else None,
            "users": usage_summary(raw.get("users")),
            "mac": text("mac"), "ip": text("ip"), "broadcast": text("broadcast"),
            "last_event_ts": _number(raw.get("last_event_ts")),
            "events_seen": seen,
        }

    @callback
    def _save_on_stop(self, _event: Any = None) -> None:
        """Home Assistant is stopping: have the latest figures written with the final writes."""
        self._unsub_stop = None
        if self.catalog["entities"]:
            self._cache_store.async_delay_save(self._cache_data, 0)

    async def async_shutdown(self) -> None:
        self._stop.set()
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        if self._ws is not None and not self._ws.closed:
            await self._close_ws(self._ws)
        for task in (self._catalog_task, self._replay_task):
            if task is not None:
                task.cancel()
        if self._unsub_recheck is not None:
            self._unsub_recheck()
            self._unsub_recheck = None
        if self.catalog["entities"]:
            try:
                await self._cache_store.async_save(self._cache_data())
            except Exception as exc:  # noqa: BLE001 - a full disk must not keep the entry from unloading
                _LOGGER.debug("%s: could not store the last known state: %s", self.pc_name, exc)
        await super().async_shutdown()

    async def _close_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Close a socket without waiting long for a PC that no longer answers."""
        try:
            await asyncio.wait_for(ws.close(), timeout=WS_CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.debug("%s did not confirm the close of the websocket", self.pc_name)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("closing the websocket to %s failed: %s", self.pc_name, exc)

    # -- REST fallback --------------------------------------------------------------------
    def _stale_limit(self) -> float:
        """Seconds without a push after which an open socket is considered dead."""
        return min(max(float(STALE_PUSH_SECONDS), STALE_PUSH_INTERVALS * self._push_interval, self._stale_after),
                   float(STALE_PUSH_MAX_SECONDS))

    async def _async_update_data(self) -> dict[str, Any]:
        limit = self._stale_limit()
        if self.ws_connected and time.monotonic() - self._last_push > limit:
            # The socket looks open but the PC stopped pushing: treat it as gone,
            # close it so the WebSocket loop reconnects, and poll for the truth.
            # Warn once; each further time in a row is quieter and waits twice as long,
            # so a PC that simply pushes rarely is not reconnected (and logged) forever.
            (_LOGGER.debug if self._stale_warned else _LOGGER.warning)(
                "No state from %s for %ds; reconnecting", self.pc_name, int(limit))
            self._stale_warned = True
            self._stale_after = min(limit * 2, float(STALE_PUSH_MAX_SECONDS))
            self.ws_connected = False
            if self._ws is not None and not self._ws.closed:
                await self._close_ws(self._ws)
        if self.ws_connected:  # pushed data is authoritative while the socket is up
            return {"pc": self.pc, "users": self.users}
        held = self.throttled_for()
        if held > 0:
            # the PC asked not to be asked before then: every request now would only be refused
            # again (and, after wrong tokens, could prolong its lockout)
            self._await_poll = False
            raise UpdateFailed(f"{self.pc_name} asked to wait {math.ceil(held)} s; not polled before then")
        try:
            health = await self.client.health()
            if not self._check_identity(health):
                raise UpdateFailed(f"a different PC ({health.get('name') or health.get('device')}) answers at {self.client.host}")
            pc = await self.client.status()
            users = await self.client.users()
        except PclinkAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except PclinkThrottledError as exc:
            self.note_throttle(exc.retry_after)
            raise UpdateFailed(str(exc)) from exc
        except PclinkApiError as exc:
            raise UpdateFailed(str(exc)) from exc
        finally:
            self._await_poll = False
        if self.ws_connected:
            # The socket came up while the answers were on their way: what it pushed is newer.
            return {"pc": self.pc, "users": self.users}
        self.health = health
        if isinstance(pc, dict):
            self.pc = pc
        if isinstance(users, dict):
            self.users = users
        self._after_state()
        return {"pc": self.pc, "users": self.users}

    @callback
    def _after_state(self) -> None:
        """New state from the PC (pushed or polled) is in ``self.pc`` / ``self.users``."""
        self._remember_addresses()
        self._check_unknown_users()
        self._check_catalog_changed()
        self._save_usage()

    # -- WebSocket ------------------------------------------------------------------------
    async def _ws_loop(self) -> None:
        while not self._stop.is_set():
            if self._reauth_in_progress():
                # Every attempt with the rejected token counts against this address on the PC
                # (it locks it out after a few): none while the user is asked for a new one.
                # Entering it reloads the entry, which starts afresh.
                await self._wait(60)
                continue
            try:
                ws = await self.client.ws_connect()
            except PclinkAuthError:
                # keep trying (slowly) once the request is dismissed: the token may be put right
                # on the PC instead of here
                self.async_start_reauth("the API token was rejected")
                await self._pause()
                continue
            except PclinkThrottledError as exc:  # 429 (wrong tokens) or 503 (no connection free): its Retry-After
                wait = max(float(exc.retry_after or 0), float(self._backoff))
                if isinstance(exc, PclinkBusyError):
                    _LOGGER.debug("%s has no connection free; next try in %d s", self.pc_name, int(wait))
                else:
                    # the lockout is for this address: commands and polls would be refused as well
                    self.note_throttle(exc.retry_after)
                    _LOGGER.warning("%s refuses connections for %d s after too many wrong tokens", self.pc_name, int(wait))
                await self._wait(wait * random.uniform(1.0, 1.2))
                self._backoff = min(self._backoff * 2, 60)
                continue
            except PclinkApiError as exc:
                _LOGGER.debug("websocket connect to %s failed: %s", self.pc_name, exc)
                await self._pause()
                continue
            except Exception:  # noqa: BLE001 - never let the push loop die; retry with backoff
                _LOGGER.exception("Unexpected error connecting to %s", self.pc_name)
                await self._pause()
                continue
            self._ws = ws
            self._ws_rejected = False
            self._identity_confirmed = False  # the hello proves it again (and clears a stale repair)
            started = time.monotonic()
            self._last_state_push = 0.0
            cancelled = False
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
                            self._handle_message_safely(data)
                        if self._ws_rejected:  # a different PC: hang up, try again later
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        break
            except asyncio.CancelledError:  # unload or Home Assistant stopping: no warning, no refresh
                cancelled = True
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                _LOGGER.debug("websocket to %s dropped: %s", self.pc_name, exc)
            except Exception:  # noqa: BLE001 - reconnect instead of ending push for good
                _LOGGER.exception("The connection to %s failed unexpectedly; reconnecting", self.pc_name)
            finally:
                self._ws = None
                # a socket that got as far as the hello counts: the PC answered and is gone again
                was_connected, self.ws_connected = self.ws_connected or self._hello_seen, False
                self._hello_seen = False
                self._missing.clear()  # "gone from the catalog" only counts while continuously connected
                quiet = cancelled or self._stop.is_set() or self.hass.is_stopping
                if not ws.closed:
                    await self._close_ws(ws)
                if was_connected and not quiet:
                    _LOGGER.warning("Lost the connection to %s; polling until it is back", self.pc_name)
                    # Until the poll has an answer the PC counts as gone: it most likely is
                    # (asleep, switched off), and the last values must not pass for current ones.
                    self._await_poll = True
                    try:
                        self.async_update_listeners()
                        await self.async_refresh()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Error while switching %s to polling", self.pc_name)
                    finally:
                        self._await_poll = False
            if time.monotonic() - started >= WS_HEALTHY_SECONDS:
                self._backoff = 1  # only a connection that held resets the backoff (not a mere handshake)
            if not self._stop.is_set():
                await self._pause()

    async def _pause(self) -> None:
        # +-20 %: several PCs (or Home Assistant instances) that lost the network together do
        # not all come back in the same second, again and again
        await self._wait(self._backoff * random.uniform(0.8, 1.2))
        self._backoff = min(self._backoff * 2, 60)

    async def _wait(self, seconds: float) -> None:
        """Sleep, but wake at once when the entry is unloaded."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _reauth_in_progress(self) -> bool:
        """Is Home Assistant asking the user for a new token for this entry right now?"""
        try:
            flows = self.hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        except Exception:  # noqa: BLE001 - never a reason to stop connecting
            return False
        return any((flow.get("context") or {}).get("source") == SOURCE_REAUTH
                   and (flow.get("context") or {}).get("entry_id") == self.entry.entry_id for flow in flows)

    @property
    def stopped(self) -> bool:
        """The entry is being (or was) unloaded."""
        return self._stop.is_set()

    @callback
    def _handle_message_safely(self, data: dict[str, Any]) -> None:
        """One bad message (or one entity that fails to update) must never end push."""
        if self._stop.is_set():  # unloading: what is still buffered must not reach torn-down platforms
            return
        try:
            self._handle_message(data)
        except Exception:  # noqa: BLE001
            kind = str(data.get("type"))
            if kind in self._logged_message_errors:
                _LOGGER.debug("error handling a %r message from %s", kind, self.pc_name, exc_info=True)
            else:
                self._logged_message_errors.add(kind)
                _LOGGER.exception("Error handling a %r message from %s (further ones are logged at debug level)",
                                  kind, self.pc_name)

    @callback
    def _note_push_interval(self, seconds: Any) -> None:
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if 0 < value <= STALE_PUSH_MAX_SECONDS:
            # slowly forget, so one long gap does not stretch the limit forever
            self._push_interval = max(value, self._push_interval * 0.9)

    @callback
    def _handle_message(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        now = time.monotonic()
        wait = _number(data.get("retry_after"))
        if data.get("ok") is False and wait is not None:
            # a request over the socket refused for throttling: not asked that way again before
            # the PC's time is up (async_request_state polls meanwhile)
            self._ws_requests_after = now + min(max(wait, 1.0), 3600.0)
            self.note_throttle(wait)
            _LOGGER.debug("%s asks not to be asked again for %d s: %s", self.pc_name, int(wait), data.get("error"))
            return
        if kind == "hello":
            status = data.get("status")
            if isinstance(status, dict) and not self._check_identity(status):
                self._ws_rejected = True
                return
        elif self._ws_rejected:
            return
        if kind in ("hello", "state", "status"):
            self._last_push = now
        if kind == "state":
            if self._last_state_push:
                gap = now - self._last_state_push
                self._note_push_interval(gap)
                if gap < STALE_PUSH_SECONDS:  # normal cadence again
                    self._stale_after = float(STALE_PUSH_SECONDS)
                    self._stale_warned = False
            self._last_state_push = now
        if kind == "hello":
            if isinstance(status, dict):
                self.health = status
                # not sent by current services; honoured when a PC announces it
                self._note_push_interval(status.get("push_interval_seconds"))
            self._note_push_interval(data.get("push_interval_seconds"))
            if isinstance(data.get("admin"), bool):
                self.admin = data["admin"]
            if data.get("writable") is False:
                # State still flows, but every command and screenshot would be refused.
                self.async_start_reauth("the PC accepted the connection read-only (the token is not valid for commands)")
            self.connections += 1
            self._hello_seen = True
            # Not "connected" yet: until the first state of this connection arrives the values
            # held are the old ones, and showing them as current would flip entities back to
            # what they were before the PC went away (and fire automations on it).
            # reachable where it is configured: an earlier "announced itself elsewhere" is moot
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(ISSUE_MOVED, self.entry.entry_id))
            self._schedule_event_replay()
            self.async_update_listeners()
        elif kind in ("state", "status"):
            if isinstance(data.get("pc"), dict):
                self.pc = data["pc"]
            if isinstance(data.get("users"), dict):
                self.users = data["users"]
            if not self.ws_connected:
                _LOGGER.debug("connected to %s", self.pc_name)
            self.ws_connected = True
            self._remember_addresses()
            self.async_set_updated_data({"pc": self.pc, "users": self.users})
            self._check_unknown_users()
            self._check_catalog_changed()
            self._save_usage()
        elif kind == "entities":
            self._apply_catalog(data)
        elif kind == "event":
            self._handle_event(data)

    # -- events ---------------------------------------------------------------------------
    @callback
    def _trigger_types(self) -> set[str]:
        known = set(USER_TRIGGERS) | set(PC_TRIGGERS)
        for types in (self.catalog.get("triggers") or {}).values():
            if isinstance(types, (dict, list)):
                known.update(str(kind) for kind in types)
        return known

    def _event_seen(self, ts: float, event: str, user: str) -> bool:
        return any(e == event and u == user and abs(t - ts) <= EVENT_SAME_SECONDS for t, e, u in self._events_seen)

    @callback
    def _handle_event(self, data: dict[str, Any], replayed: bool = False) -> None:
        payload = {k: v for k, v in data.items() if k != "type"}
        # the PC's event list calls the type "kind"; pushed events call it "event"
        event = str(payload.get("event") or payload.get("kind") or "")
        if not event:
            return
        payload["event"] = event
        ts = _number(payload.get("ts"))
        if ts is not None:
            user = str(payload.get("user") or "")
            if replayed and self._event_seen(ts, event, user):
                return
            self._events_seen.append((ts, event, user))
            if self.last_event_ts is None or ts > self.last_event_ts:
                self.last_event_ts = ts
            self._cache_store.async_delay_save(self._cache_data, 30)
        if replayed:
            payload["replayed"] = True
        self.hass.bus.async_fire(EVENT_TYPE, {**payload, "device_id": self.device_id, "pc_id": self.pc_id,
                                              "pc_name": self.pc_name, "entry_id": self.entry.entry_id})
        if event in CATALOG_EVENTS and not replayed:
            self._schedule_catalog_refresh()

    @callback
    def _schedule_event_replay(self) -> None:
        if self._stop.is_set() or (self._replay_task is not None and not self._replay_task.done()):
            return
        self._replay_task = self.entry.async_create_background_task(
            self.hass, self._replay_events(), name=f"{DOMAIN} events {self.pc_id}")

    async def _replay_events(self) -> None:
        """Fire what the PC recorded while no socket was open (Home Assistant restarting, the
        network gone, the PC booting - its "started" is always earlier than our connection).

        Bounded: only event types that are also pushed live, nothing older than a day,
        nothing already seen, at most EVENT_REPLAY_MAX.  Before the first connection ever
        there is no "missed", so nothing is replayed then."""
        since = self.last_event_ts
        if since is None:
            self.last_event_ts = time.time()
            self._cache_store.async_delay_save(self._cache_data, 30)
            return
        try:
            self._refuse_wrong_pc()
            events = await self.client.events(EVENT_REPLAY_MAX, since=since, announced=True)
        except PclinkApiError as exc:
            _LOGGER.debug("could not ask %s for the events it recorded: %s", self.pc_name, exc)
            return
        known = self._trigger_types()
        now = time.time()
        since = min(since, now + 3600)  # a PC clock that was once far ahead must not silence replays for good
        missed: list[tuple[float, dict[str, Any]]] = []
        for item in events:
            ts = _number(item.get("ts"))
            kind = str(item.get("event") or item.get("kind") or "")
            if ts is None or ts <= since or ts < now - EVENT_REPLAY_MAX_AGE_SECONDS or kind not in known:
                continue
            missed.append((ts, {k: v for k, v in item.items() if v is not None}))
        missed.sort(key=lambda pair: pair[0])
        for _ts, item in missed[-EVENT_REPLAY_MAX:]:
            self._handle_event(item, replayed=True)
        if missed:
            _LOGGER.debug("%s: %d recorded event(s) looked at for replay", self.pc_name, len(missed))

    # -- commands -------------------------------------------------------------------------
    def throttled_for(self) -> float:
        """Seconds the PC still asked us to wait before it is sent anything (0: none)."""
        return max(0.0, self._hold_until - time.monotonic())

    @callback
    def note_throttle(self, seconds: Any) -> None:
        """The PC asked to wait ``seconds`` (a ``retry_after`` or Retry-After): no command and
        no poll goes to it before then.  Only ever extended, 1 s to 1 h."""
        wait = _number(seconds)
        if wait is None:
            return
        until = time.monotonic() + min(max(wait, 1.0), 3600.0)
        if until > self._hold_until:
            self._hold_until = until
            _LOGGER.info("%s asked to wait %d s; nothing is sent to it before then", self.pc_name, math.ceil(wait))

    def _refuse_held(self) -> None:
        """Fail fast while the PC's wait is not up - nothing is sent."""
        held = self.throttled_for()
        if held > 0:
            raise PclinkThrottledError(f"{self.pc_name} asked to wait {math.ceil(held)} s; nothing was sent", held)

    def throttled_error(self, exc: PclinkThrottledError) -> HomeAssistantError:
        """The translated error for a command the PC refused with a wait, or that was held back."""
        seconds = str(max(1, math.ceil(float(exc.retry_after or self.throttled_for() or 1))))
        return HomeAssistantError(
            f"{self.pc_name} asked to wait {seconds} s before it takes commands again ({exc})",
            translation_domain=DOMAIN, translation_key="throttled",
            translation_placeholders={"pc": self.pc_name, "seconds": seconds})

    async def async_command(self, data: dict[str, Any]) -> dict[str, Any]:
        """Run a JSON command on the PC; raises a PclinkApiError when it refuses or cannot be reached
        (PclinkThrottledError, without sending, while the PC's ``retry_after`` is not up)."""
        self._refuse_wrong_pc()
        self._refuse_held()
        try:
            return await self.client.command(data)
        except PclinkThrottledError as exc:
            self.note_throttle(exc.retry_after)
            raise

    async def async_send(self, data: dict[str, Any]) -> dict[str, Any]:
        """Like :meth:`async_command`, but returns the PC's ``{ok, error}`` instead of raising on a refusal."""
        self._refuse_wrong_pc()
        self._refuse_held()
        try:
            result = await self.client.send(data)
        except PclinkThrottledError as exc:
            self.note_throttle(exc.retry_after)
            raise
        self.note_throttle(reply_retry_after(result))
        return result

    async def async_request_state(self) -> None:
        """Get the state as it is after a command: asked for over the open socket (the answer
        arrives as a push), by a poll when there is none."""
        ws = self._ws
        if ws is not None and not ws.closed and (self.ws_connected or self._hello_seen) \
                and time.monotonic() >= self._ws_requests_after:
            self._request_id += 1
            try:
                await self.client.ws_request(ws, "status", self._request_id)
                return
            except Exception as exc:  # noqa: BLE001 - the socket is going down; the loop notices
                _LOGGER.debug("could not ask %s for its state: %s", self.pc_name, exc)
        await self.async_request_refresh()

    # -- catalog --------------------------------------------------------------------------
    @callback
    def _cache_data(self) -> dict[str, Any]:
        return {"health": self.health, "catalog": self.catalog, "mac": self.wake_mac, "ip": self.wake_ip,
                "broadcast": self.wake_broadcast, "users": usage_summary(self.users),
                "last_event_ts": self.last_event_ts,
                "events_seen": [list(item) for item in list(self._events_seen)[-_EVENTS_STORED:]]}

    @callback
    def _save_usage(self) -> None:
        """Store the users' figures now and then (and at once when a day starts): a PC that is
        off when Home Assistant restarts still counts with what it reported for today."""
        dates = tuple(sorted({str(p.get("date")) for p in self.users.values() if isinstance(p, dict) and p.get("date")}))
        now = time.monotonic()
        if dates == self._usage_saved_dates and now - self._last_usage_save < USAGE_CACHE_SECONDS:
            return
        self._usage_saved_dates = dates
        self._last_usage_save = now
        self._cache_store.async_delay_save(self._cache_data, 10)

    @callback
    def _remember_addresses(self) -> None:
        """Keep the PC's MAC / IP / broadcast address for Wake-on-LAN (the PC cannot tell us once it is off)."""
        mac, ip, broadcast = self.pc.get("mac"), self.pc.get("ip"), self.pc.get("broadcast")
        if not (isinstance(mac, str) and mac):
            return
        ip = ip if isinstance(ip, str) and ip else self.wake_ip
        if isinstance(broadcast, str) and broadcast:
            pass
        elif ip != self.wake_ip:
            broadcast = None  # another network: the old broadcast address no longer applies
        else:
            broadcast = self.wake_broadcast
        if (mac, ip, broadcast) != (self.wake_mac, self.wake_ip, self.wake_broadcast):
            self.wake_mac, self.wake_ip, self.wake_broadcast = mac, ip, broadcast
            self._cache_store.async_delay_save(self._cache_data, 10)

    def _apply_catalog(self, cat: dict[str, Any], notify: bool = True) -> None:
        if self._stop.is_set():  # unloading: the platforms that would take new entities are going
            return
        entities =[e for e in (cat.get("entities") or []) if isinstance(e, dict) and e.get("key") and e.get("platform")]
        self.catalog = {"device": cat.get("device") or {}, "users": cat.get("users") or {},
                        "entities": entities, "triggers": cat.get("triggers") or {}, "version": cat.get("version"),
                        "hash": cat.get("hash")}
        if notify:
            self._cache_store.async_delay_save(self._cache_data, 10)
        self.catalog_keys = {e["key"] for e in entities}
        self._last_catalog_refresh = time.monotonic()
        # Users the PC reports state for but (deliberately) lists no entities for: remember
        # them, so their presence in every push does not trigger a refresh each time.
        self._absent_users = {user for user in self.users if user not in self.catalog["users"]}
        new: list[dict[str, Any]] = []
        changed = 0
        for spec in entities:
            entity = self.live.get(spec["key"])
            if entity is None:
                new.append(spec)
            elif entity.spec != spec:
                try:
                    entity.async_update_spec(spec)  # renamed, new limits / unit / options, ...
                    changed += 1
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("%s: could not apply the changed definition of %s", self.pc_name, spec["key"])
        if new and notify:
            _LOGGER.debug("%s: %d new entities", self.pc_name, len(new))
            async_dispatcher_send(self.hass, self.signal_new_entities, new)
        if changed:
            _LOGGER.debug("%s: %d entity definitions changed", self.pc_name, changed)
        self._reconcile_registry()
        if notify:
            self.async_update_listeners()  # entities whose key left the catalog turn unavailable now
        if self.device_id:
            try:
                dr.async_get(self.hass).async_update_device(self.device_id, name=self.pc_name,
                                                            sw_version=self.device_info.get("sw_version"))
            except Exception as exc:  # noqa: BLE001 - e.g. the device was deleted by hand
                _LOGGER.debug("%s: device registry update failed: %s", self.pc_name, exc)

    @callback
    def _reconcile_registry(self) -> None:
        """Remove registry entries the catalog no longer has (a deleted user, rule, app, reward ...).

        Nothing is removed on first sight: the entry must stay absent for
        CATALOG_REMOVE_GRACE_SECONDS, confirmed by a fresh catalog, with the PC
        connected the whole time.  Until then the entity is merely unavailable."""
        if not self.catalog["entities"]:  # an empty catalog is a fault, not a reason to delete everything
            self._missing.clear()
            self._arm_recheck()
            return
        wanted = {self.unique_id_for(e) for e in self.catalog["entities"]}
        wanted.add(self.wake_unique_id)  # provided by the integration itself, not by the catalog
        registry = er.async_get(self.hass)
        now = time.monotonic()
        seen: set[str] = set()
        for reg_entry in er.async_entries_for_config_entry(registry, self.entry.entry_id):
            uid = reg_entry.unique_id
            if uid in wanted or uid.startswith(ROLLUP_UNIQUE_PREFIX):  # the roll-up is not the PC's to withdraw
                continue
            seen.add(uid)
            first = self._missing.setdefault(uid, now)
            if now - first < CATALOG_REMOVE_GRACE_SECONDS:
                continue
            _LOGGER.info("%s: removing %s, which the PC no longer provides", self.pc_name, reg_entry.entity_id)
            seen.discard(uid)
            for key, entity in list(self.live.items()):
                if getattr(entity, "unique_id", None) == uid:
                    self.live.pop(key, None)
            registry.async_remove(reg_entry.entity_id)
        self._missing = {uid: first for uid, first in self._missing.items() if uid in seen}
        self._arm_recheck()

    @callback
    def _arm_recheck(self) -> None:
        if self._unsub_recheck is not None:
            self._unsub_recheck()
            self._unsub_recheck = None
        if not self._missing or self._stop.is_set():
            return
        due = min(self._missing.values()) + CATALOG_REMOVE_GRACE_SECONDS - time.monotonic()
        self._unsub_recheck = async_call_later(self.hass, max(due, 0) + 5, self._recheck_missing)

    @callback
    def _recheck_missing(self, _now: Any = None) -> None:
        self._unsub_recheck = None
        self._schedule_catalog_refresh()  # confirm with a fresh catalog before anything is removed

    def catalog_entities(self, platform: Optional[str] = None) -> list[dict[str, Any]]:
        return [e for e in self.catalog["entities"] if platform is None or e["platform"] == platform]

    @callback
    def _check_unknown_users(self) -> None:
        known = self.catalog["users"]
        if any(user not in known and user not in self._absent_users for user in self.users):
            self._schedule_catalog_refresh()

    def _state_catalog_signature(self) -> tuple[str, bool]:
        """``(signature, differs)``: what the state says about the PC's set of entities and
        whether the catalog we hold is a different one.

        A current service puts the catalog's ``hash`` into every state (``catalog_hash``).
        An older one does not; there the parts of the state that follow the hardware -
        the hardware sensors and the disks, which appear some time after the PC boots or
        when a drive is added - are compared with what the catalog reads."""
        state_hash = self.pc.get("catalog_hash")
        if isinstance(state_hash, str) and state_hash:
            return state_hash, self.catalog.get("hash") != state_hash
        read = {str(e.get("value")) for e in self.catalog["entities"] if e.get("source") != "user"}
        unread = sorted(key for key in self.pc if isinstance(key, str) and key not in read and (
            (key.startswith("hw_") and key != "hw_sensor_count") or (key.startswith("disk_") and key.endswith("_percent"))))
        count = self.pc.get("hw_sensor_count")
        listed = sum(1 for value in read if value.startswith("hw_"))
        differs = bool(unread) or (isinstance(count, int) and not isinstance(count, bool) and count != listed)
        return f"legacy:{count}:{','.join(unread)}", differs

    @callback
    def _check_catalog_changed(self) -> None:
        """Fetch the catalog when the state shows the PC now has a different set of entities.
        Once per signature: a PC whose two sides never agree is not asked again and again."""
        signature, differs = self._state_catalog_signature()
        if not differs:
            self._catalog_tried = None
        elif signature != self._catalog_tried:
            self._catalog_tried = signature
            self._schedule_catalog_refresh()

    @callback
    def _schedule_catalog_refresh(self) -> None:
        if self._stop.is_set():
            return
        if self._catalog_task is not None and not self._catalog_task.done():
            # The running one may already hold an answer from before this change: go round again.
            self._catalog_again = True
            return
        # A background task: it sleeps, and must not hold up Home Assistant's start or stop.
        self._catalog_task = self.entry.async_create_background_task(
            self.hass, self._refresh_catalog(), name=f"{DOMAIN} catalog {self.pc_id}")

    async def _refresh_catalog(self) -> None:
        while True:
            self._catalog_again = False
            wait = self._catalog_min_wait - (time.monotonic() - self._last_catalog_refresh)
            if wait > 0:
                await asyncio.sleep(wait)
            if not await self._fetch_catalog() or not self._catalog_again or self._stop.is_set():
                return

    async def _fetch_catalog(self) -> bool:
        try:
            self._refuse_wrong_pc()
            catalog = await self.client.entities()
        except PclinkAuthError:
            self.async_start_reauth("the API token was rejected")
            return False
        except PclinkApiError as exc:
            _LOGGER.debug("catalog refresh for %s failed: %s", self.pc_name, exc)
            self._catalog_refresh_failed()
            return False
        if self.wrong_pc is not None:  # found out while the answer was on its way
            return False
        try:
            self._apply_catalog(catalog)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: could not apply the entity catalog", self.pc_name)
            self._catalog_refresh_failed()
            return False
        self._catalog_min_wait = float(CATALOG_REFRESH_MIN_SECONDS)
        return True

    @callback
    def _catalog_refresh_failed(self) -> None:
        """Back off (20 s, 40 s ... 10 min) and restart the removal grace period."""
        self._last_catalog_refresh = time.monotonic()
        self._catalog_min_wait = min(self._catalog_min_wait * 2, float(CATALOG_REFRESH_MAX_SECONDS))
        self._catalog_tried = None  # the next state asks again (after the wait above)
        self._missing.clear()
        self._arm_recheck()

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
