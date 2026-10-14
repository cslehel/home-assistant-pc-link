"""The "PC Link (all PCs)" device: every PC's users rolled up into one view.

With two or more PCs configured, Home Assistant already holds each PC's state,
so the roll-up is worked out here: per user the time used today across all
PCs, the PCs they are on, and how many PCs are online and in use.

The entities belong to one of the PC entries - the first one set up while at
least two PCs are configured.  When that entry goes away, another one takes
over (see ``__init__.py``).
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import PclinkApiError, PclinkAuthError
from .const import CATALOG_REMOVE_GRACE_SECONDS, DOMAIN, MANUFACTURER, ROLLUP_DEVICE_ID, ROLLUP_UNIQUE_PREFIX

_LOGGER = logging.getLogger(__name__)

DATA_KEY = f"{DOMAIN}_rollup"
SIGNAL_NEW_USERS = f"{DOMAIN}_rollup_new_users"
MIN_PCS = 2   # a roll-up of a single PC would only repeat that PC's entities
PEER_PUSH_SECONDS = 30      # a PC is told about changed usage on the other PCs at most this often
PEER_REPEAT_SECONDS = 120   # ... and at least this often, changed or not
PEER_RETRY_MAX_SECONDS = 600  # a PC that keeps failing to take it is tried again at most this far apart

STATUS_OPTIONS = ["logged_off", "active", "idle", "away", "locked", "disconnected"]
_STATUS_RANK = {status: rank for rank, status in enumerate(["active", "idle", "away", "locked", "disconnected", "logged_off"])}
_PRESENCE_RANK = {presence: rank for rank, presence in enumerate(["active", "passive", "idle", "away", "none"])}


def _minutes(value: Any) -> float:
    """A minutes figure from a PC's payload: a finite number from 0 to one day, else 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return 0.0
    return min(float(value), 1440.0)


def build_rollup(pcs: list[dict[str, Any]], today: str) -> dict[str, Any]:
    """Combine the PCs' user payloads.  Pure: ``pcs`` is a list of
    ``{"id", "name", "online", "users": {user: payload}}`` and ``today`` an ISO date.

    A PC that is off still counts with the minutes it reported for today; where
    its users are and what they are doing is only taken from PCs that are online."""
    out_pcs: dict[str, dict[str, Any]] = {}
    users: dict[str, dict[str, Any]] = {}
    # Everything below is keyed by the PC's name; two PCs that carry the same one are told
    # apart by their id, or one would hide the other.
    names = [str(pc.get("name") or pc["id"]) for pc in pcs]
    for pc, name in zip(pcs, names):
        pc_id, online = str(pc["id"]), bool(pc.get("online"))
        label = f"{name} ({pc_id})" if names.count(name) > 1 else name
        info = out_pcs[pc_id] = {"online": online, "name": label, "users_active": [], "users_logged_in": []}
        for user, payload in (pc.get("users") or {}).items():
            if not isinstance(payload, dict):
                continue
            # Minutes count only for today: a PC whose clock (or last report) is on another day
            # says nothing about today, online or not.  A payload without a date (an old
            # service) is taken as today's while the PC is online.
            date = payload.get("date")
            counts = date == today or (online and date is None)
            if not (online or counts):
                continue
            user = str(user).lower()
            name = payload.get("name")
            entry = users.setdefault(user, {
                "user": user, "name": name if isinstance(name, str) and name else user,
                "total_minutes_today": 0.0, "gaming_minutes": 0.0, "browsing_minutes": 0.0, "per_pc": {},
                "active_on": [], "logged_in_on": [], "status": "logged_off", "presence": "none",
                "remaining_minutes": None, "managed_on": [],
            })
            if counts:
                used = _minutes(payload.get("used_minutes"))
                entry["total_minutes_today"] += used
                entry["gaming_minutes"] += _minutes(payload.get("gaming_minutes"))
                entry["browsing_minutes"] += _minutes(payload.get("browsing_minutes"))
                entry["per_pc"][info["name"]] = round(used, 1)
            if not online:
                continue
            status = str(payload.get("status") or "logged_off")
            if payload.get("logged_in"):
                entry["logged_in_on"].append(info["name"])
                info["users_logged_in"].append(user)
            if status == "active":
                entry["active_on"].append(info["name"])
                info["users_active"].append(user)
            if _STATUS_RANK.get(status, 9) < _STATUS_RANK.get(entry["status"], 9):
                entry["status"] = status
            presence = str(payload.get("presence") or "none")
            if _PRESENCE_RANK.get(presence, 9) < _PRESENCE_RANK.get(entry["presence"], 9):
                entry["presence"] = presence
            if payload.get("managed"):
                entry["managed_on"].append(info["name"])
                left = payload.get("remaining_minutes")
                if isinstance(left, (int, float)) and not isinstance(left, bool) and math.isfinite(left):
                    left = max(0.0, float(left))
                    entry["remaining_minutes"] = left if entry["remaining_minutes"] is None else min(entry["remaining_minutes"], left)
    for entry in users.values():
        for key in ("total_minutes_today", "gaming_minutes", "browsing_minutes"):
            entry[key] = round(entry[key], 1)
        for key in ("active_on", "logged_in_on", "managed_on"):
            entry[key].sort()
    online_list = sorted(p["name"] for p in out_pcs.values() if p["online"])
    in_use = sorted(p["name"] for p in out_pcs.values() if p["online"] and p["users_active"])
    return {
        "pcs": {p["name"]: {k: v for k, v in p.items() if k != "name"} for p in out_pcs.values()},
        "pcs_online": len(online_list), "pcs_online_list": online_list,
        "pcs_in_use": len(in_use), "pcs_in_use_list": in_use, "any_pc_in_use": bool(in_use),
        "users": users, "active_users": sorted(u for u, e in users.items() if e["active_on"]),
    }


# What a PC is told about a user on another PC: the day, the minutes used, and what was
# granted there - bonus minutes and tonight's bedtime extension as that PC itself holds them
# (the "local" figures: never what it was told by others, or the PCs would add them up in a circle).
PEER_USAGE_KEYS = ("name", "date", "used_minutes", "history", "bonus_local_minutes", "bedtime_extension_local_minutes")


def peer_usage(users: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What one PC's users did, as another PC needs it for a shared budget.  A payload
    without a date cannot be put on a day and is left out."""
    out: dict[str, dict[str, Any]] = {}
    for user, payload in (users or {}).items():
        if isinstance(payload, dict) and isinstance(payload.get("date"), str):
            out[str(user)] = {key: payload[key] for key in PEER_USAGE_KEYS if key in payload}
    return out


class RollupHub:
    """Listens to every PC's coordinator and keeps the combined view up to date."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.owner_entry_id: Optional[str] = None   # the config entry the roll-up entities belong to
        self.data: dict[str, Any] = build_rollup([], "")
        self._coordinators: dict[str, Any] = {}
        self._unsub: dict[str, CALLBACK_TYPE] = {}
        self._listeners: list[Callable[[], None]] = []
        self._announced: set[str] = set()
        self._added: dict[str, set[str]] = {}        # platform -> users it has entities for
        self._gone_since: dict[str, float] = {}      # user no PC reports -> since when
        self._peer_sent: dict[str, tuple[int, str, float]] = {}   # entry id -> (connection, what was sent, when)
        self._peer_retry: dict[str, tuple[int, float]] = {}       # entry id -> (failures in a row, not before)
        self._peer_warned: set[str] = set()                        # entry ids whose failure was logged as a warning
        self._peer_timer: Optional[CALLBACK_TYPE] = None

    @callback
    def attach(self, coordinator: Any) -> None:
        entry_id = coordinator.entry.entry_id
        self.detach(entry_id)
        self._coordinators[entry_id] = coordinator
        self._unsub[entry_id] = coordinator.async_add_listener(self._changed)
        self._changed()

    @callback
    def detach(self, entry_id: str) -> None:
        unsub = self._unsub.pop(entry_id, None)
        if unsub is not None:
            unsub()
        self._peer_sent.pop(entry_id, None)
        self._peer_retry.pop(entry_id, None)
        if self._coordinators.pop(entry_id, None) is not None:
            self._changed()
        if not self._coordinators:
            self.stop()

    @callback
    def release(self, entry_id: str) -> bool:
        """The entry is being unloaded; True when it was the one holding the roll-up entities."""
        if self.owner_entry_id != entry_id:
            return False
        self.owner_entry_id = None
        self._announced.clear()
        self._added.clear()
        return True

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)

        @callback
        def remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return remove

    @callback
    def _changed(self) -> None:
        try:
            self.data = build_rollup(
                [{"id": c.pc_id, "name": c.pc_name, "online": c.online, "users": c.users} for c in self._coordinators.values()],
                dt_util.now().date().isoformat())
        except Exception:  # noqa: BLE001 - a malformed payload must not stop the PCs' own updates
            _LOGGER.exception("Could not work out the roll-up of all PCs")
            return
        try:
            self._drop_gone_users()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not remove the roll-up entities of users that are gone")
        new = sorted(set(self.data["users"]) - self._announced)
        if new and self.owner_entry_id is not None:
            self._announced.update(new)
            async_dispatcher_send(self.hass, SIGNAL_NEW_USERS, new)
        for update in list(self._listeners):
            update()
        try:
            self._push_peer_usage()
        except Exception:  # noqa: BLE001 - never at the cost of the PCs' own updates
            _LOGGER.exception("Could not pass the PCs each other's usage")

    def user(self, user: str) -> Optional[dict[str, Any]]:
        return self.data["users"].get(user)

    @callback
    def _drop_gone_users(self) -> None:
        """Remove the entities of a user no PC knows any more (deleted, renamed).

        Only on full knowledge: every configured PC is loaded and connected.  A user of a
        PC that is switched off is still known (its users are remembered), and one that is
        merely missing from today's figures stays, unavailable."""
        if not self._announced or self.owner_entry_id is None:
            return
        known = {str(user).lower() for c in self._coordinators.values() for user in c.users}
        gone = self._announced - known - set(self.data["users"])
        if not gone or len(self._coordinators) < configured_pcs(self.hass) or not all(c.online for c in self._coordinators.values()):
            self._gone_since.clear()
            return
        # ... and not on first sight: the same patience a PC's own entities get
        now = time.monotonic()
        self._gone_since = {user: self._gone_since.get(user, now) for user in gone}
        gone = {user for user, since in self._gone_since.items() if now - since >= CATALOG_REMOVE_GRACE_SECONDS}
        if not gone:
            return
        for user in gone:
            self._gone_since.pop(user, None)
        _LOGGER.info("Removing the roll-up entities of %s, whom no PC reports any more", ", ".join(sorted(gone)))
        remove_rollup_entities(self.hass, gone)
        self._announced -= gone
        for added in self._added.values():
            added.difference_update(gone)

    # -- shared budgets: every PC is told what its users did on the other PCs -----------------
    @callback
    def _push_peer_usage(self) -> None:
        """Send the PCs that keep a shared budget (``time_limits.shared_budget``) the usage
        of the other PCs.  A PC is sent to when the figures changed, at most every
        PEER_PUSH_SECONDS; again on every new connection (it may have restarted and
        forgotten them) and every PEER_REPEAT_SECONDS in any case."""
        now = time.monotonic()
        wait: Optional[float] = None
        for entry_id, target in self._coordinators.items():
            if not target.pc.get("shared_budget") or not target.online:
                self._peer_sent.pop(entry_id, None)
                self._peer_retry.pop(entry_id, None)
                continue
            retry = self._peer_retry.get(entry_id)
            if retry is not None and now < retry[1]:  # the last send failed: not again before its time
                wait = retry[1] - now if wait is None else min(wait, retry[1] - now)
                continue
            peers = {other.pc_id: {"name": other.pc_name, "users": peer_usage(other.users)}
                     for other_id, other in self._coordinators.items() if other_id != entry_id and other.pc_id != target.pc_id}
            signature = json.dumps(peers, sort_keys=True, default=str)
            sent = self._peer_sent.get(entry_id)
            if sent is not None and sent[0] == target.connections:
                age = now - sent[2]
                due = PEER_REPEAT_SECONDS if sent[1] == signature else PEER_PUSH_SECONDS
                if age < due:
                    wait = due - age if wait is None else min(wait, due - age)
                    continue
            self._peer_sent[entry_id] = (target.connections, signature, now)
            wait = PEER_REPEAT_SECONDS if wait is None else min(wait, PEER_REPEAT_SECONDS)
            # the entry's task: cancelled with it when the PC is unloaded
            target.entry.async_create_background_task(
                self.hass, self._send_peer_usage(entry_id, target, peers), name=f"{DOMAIN} peer usage {target.pc_id}",
                eager_start=True)  # older Home Assistant (2024.3) started it lazily by default
        if self._peer_timer is not None:
            self._peer_timer()
            self._peer_timer = None
        if wait is not None:
            self._peer_timer = async_call_later(self.hass, max(wait, 0) + 0.5, self._peer_timer_fired)

    @callback
    def _peer_timer_fired(self, _now: Any = None) -> None:
        self._peer_timer = None
        self._push_peer_usage()

    async def _send_peer_usage(self, entry_id: str, target: Any, peers: dict[str, Any]) -> None:
        try:
            result = await target.async_send({"action": "peer_usage", "peers": peers})
        except PclinkApiError as exc:
            if isinstance(exc, PclinkAuthError):
                target.async_start_reauth("the API token was rejected for a command")
            self._peer_failed(entry_id, target, f"could not pass the other PCs' usage to {target.pc_name} ({exc})",
                              getattr(exc, "retry_after", None))
            return
        if not result.get("ok"):
            # refused (an older or misconfigured PC, or throttled): retried like a failure, not every 30 s forever
            self._peer_failed(entry_id, target, f"{target.pc_name} did not take the other PCs' usage: {result.get('error')}",
                              result.get("retry_after"))
            return
        self._peer_retry.pop(entry_id, None)
        self._peer_warned.discard(entry_id)

    @callback
    def _peer_failed(self, entry_id: str, target: Any, reason: str, retry_after: Any = None) -> None:
        """Try again, but later each time (30 s, 1 min ... 10 min) - and not before the PC's
        ``retry_after`` when it gave one: the PC's state keeps arriving every few seconds and
        must not turn into as many failing requests.  Logged as a warning once (until it
        works again), then at debug level."""
        failures = self._peer_retry.get(entry_id, (0, 0.0))[0] + 1
        delay = min(PEER_PUSH_SECONDS * 2 ** (failures - 1), PEER_RETRY_MAX_SECONDS)
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and math.isfinite(retry_after):
            delay = max(delay, min(float(retry_after), 3600.0))
        if entry_id in self._peer_warned:
            _LOGGER.debug("%s; next try in %d s", reason, delay)
        else:
            self._peer_warned.add(entry_id)
            _LOGGER.warning("%s; trying again later (further failures are logged at debug level)", reason)
        if self._coordinators.get(entry_id) is target:
            self._peer_retry[entry_id] = (failures, time.monotonic() + delay)
            self._peer_sent.pop(entry_id, None)
            self._push_peer_usage()  # arms the timer for the retry

    @callback
    def stop(self, _event: Any = None) -> None:
        if self._peer_timer is not None:
            self._peer_timer()
            self._peer_timer = None


@callback
def get_hub(hass: HomeAssistant) -> RollupHub:
    hub = hass.data.get(DATA_KEY)
    if hub is None:
        hub = hass.data[DATA_KEY] = RollupHub(hass)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, hub.stop)
    return hub


def configured_pcs(hass: HomeAssistant, leaving: Optional[str] = None) -> int:
    """PCs set up, not counting ``leaving`` - an entry being removed, which older Home
    Assistant (2024.3) still lists while ``async_remove_entry`` runs."""
    return sum(1 for entry in hass.config_entries.async_entries(DOMAIN)
               if entry.source != "ignore" and not entry.disabled_by and entry.entry_id != leaving)


@callback
def remove_rollup_entities(hass: HomeAssistant, users: Optional[Iterable[str]] = None) -> int:
    """Take roll-up entities out of the entity registry: those of ``users``, or - with
    None - all of them together with the "all PCs" device (fewer than two PCs are left).
    Entities that are loaded go with their registry entry.  Returns how many were removed."""
    registry = er.async_get(hass)
    prefixes = None if users is None else tuple(f"{ROLLUP_UNIQUE_PREFIX}user:{user}:" for user in users)
    removed = 0
    for reg_entry in list(registry.entities.values()):
        if reg_entry.platform != DOMAIN or not reg_entry.unique_id.startswith(ROLLUP_UNIQUE_PREFIX):
            continue
        if prefixes is not None and not reg_entry.unique_id.startswith(prefixes):
            continue
        registry.async_remove(reg_entry.entity_id)
        removed += 1
    if users is None:
        devices = dr.async_get(hass)
        # found through the entries: the registry's lookup by identifier changed with Home Assistant's releases
        found = {device.id for entry in hass.config_entries.async_entries(DOMAIN)
                 for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
                 if (DOMAIN, ROLLUP_DEVICE_ID) in device.identifiers}
        for device_id in found:
            devices.async_remove_device(device_id)
    return removed


class _RollupEntity(Entity):
    """Base: one value of the roll-up, optionally of one user."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: RollupHub, key: str, name: str, user: Optional[str] = None, icon: Optional[str] = None) -> None:
        self.hub = hub
        self.user = user
        self._key = key
        label = (hub.user(user) or {}).get("name") or user if user else None
        self._attr_name = f"{label} {name}" if label else name
        self._attr_icon = icon
        self._attr_unique_id = ROLLUP_UNIQUE_PREFIX + (f"user:{user}:{key}" if user else key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ROLLUP_DEVICE_ID)}, name="PC Link (all PCs)", manufacturer=MANUFACTURER,
            model="Roll-up of all PCs", entry_type=DeviceEntryType.SERVICE)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hub.async_add_listener(self.async_write_ha_state))

    @property
    def _source(self) -> Optional[dict[str, Any]]:
        return self.hub.user(self.user) if self.user else self.hub.data

    @property
    def available(self) -> bool:
        return self._source is not None

    @property
    def _value(self) -> Any:
        source = self._source
        return None if source is None else source.get(self._key)


class RollupSensor(_RollupEntity, SensorEntity):
    def __init__(self, hub: RollupHub, key: str, name: str, user: Optional[str] = None, icon: Optional[str] = None, *,
                 minutes: bool = False, options: Optional[list[str]] = None, measurement: bool = False,
                 attributes: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None) -> None:
        super().__init__(hub, key, name, user, icon)
        self._attributes = attributes
        self._minutes = minutes
        if minutes:
            # A total that starts anew every day (``last_reset``).  Not "total increasing":
            # the sum may go down a little within a day - a PC that reports again after it
            # was unreachable, a stored figure that is a few minutes old - and that must
            # read as a correction, not as a new day with all of it used a second time.
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = "min"
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_suggested_display_precision = 0
        elif options:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = options
        elif measurement:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Any:
        value = self._value
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)[:255] or "none"
        if getattr(self, "_attr_options", None) and value not in self._attr_options:
            return None
        return value

    @property
    def last_reset(self) -> Optional[datetime]:
        """Midnight of the day the minutes are for (Home Assistant's day, like the roll-up's)."""
        return dt_util.start_of_local_day() if self._minutes else None

    @property
    def extra_state_attributes(self) -> Optional[dict[str, Any]]:
        source = self._source
        if source is None or self._attributes is None:
            return None
        return self._attributes(source)


class RollupBinarySensor(_RollupEntity, BinarySensorEntity):
    def __init__(self, hub: RollupHub, key: str, name: str, user: Optional[str] = None, *,
                 device_class: Optional[BinarySensorDeviceClass] = None) -> None:
        super().__init__(hub, key, name, user)
        self._attr_device_class = device_class

    @property
    def is_on(self) -> Optional[bool]:
        value = self._value
        return None if value is None else bool(value)


def _entities(hub: RollupHub, platform: str, user: Optional[str]) -> list[Entity]:
    if platform == "binary_sensor":
        if user is None:
            return [RollupBinarySensor(hub, "any_pc_in_use", "Any PC in use", device_class=BinarySensorDeviceClass.OCCUPANCY)]
        return [RollupBinarySensor(hub, "logged_in_on", "logged in anywhere", user, device_class=BinarySensorDeviceClass.PRESENCE)]
    if user is None:
        return [
            RollupSensor(hub, "pcs_online", "PCs online", icon="mdi:desktop-tower-monitor", measurement=True,
                         attributes=lambda d: {"pcs": d["pcs"]}),
            RollupSensor(hub, "pcs_in_use", "PCs in use", icon="mdi:account-multiple-check", measurement=True,
                         attributes=lambda d: {"pcs": d["pcs_in_use_list"], "active_users": d["active_users"]}),
        ]
    return [
        RollupSensor(hub, "total_minutes_today", "total time today", user, "mdi:timer-sand", minutes=True,
                     attributes=lambda d: {k: v for k, v in d.items() if k not in ("user", "total_minutes_today")}),
        RollupSensor(hub, "status", "status (all PCs)", user, "mdi:account", options=STATUS_OPTIONS),
        RollupSensor(hub, "active_on", "active on", user, "mdi:monitor-account"),
    ]


@callback
def async_setup_rollup_entities(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
                                platform: str) -> None:
    """Called by the sensor and binary_sensor platforms of every entry; only the owner's adds anything."""
    hub = get_hub(hass)
    if hub.owner_entry_id != entry.entry_id:
        return
    added = hub._added[platform] = set()

    @callback
    def _add_users(users: list[str]) -> None:
        if hub.owner_entry_id != entry.entry_id:  # released: this entry is being unloaded
            return
        fresh =[user for user in users if user not in added]
        added.update(fresh)
        if fresh:
            async_add_entities([entity for user in fresh for entity in _entities(hub, platform, user)])

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_USERS, _add_users))
    async_add_entities(_entities(hub, platform, None))
    hub._announced.update(hub.data["users"])
    _add_users(sorted(hub.data["users"]))
