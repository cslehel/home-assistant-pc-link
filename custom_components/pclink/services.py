"""Home Assistant actions (services) for PC Link: ``pclink.add_minutes``, ``pclink.focus``, ...

Every action targets one PC (``device_id``; optional when only one PC is
configured) and is sent as the JSON command the PC's local API understands.
Refusals - typically "parent pin required" - surface as errors in the UI.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .api import PclinkApiError, PclinkCommandError
from .const import DOMAIN
from .coordinator import PclinkCoordinator

_LOGGER = logging.getLogger(__name__)

ATTR_DEVICE_ID = "device_id"
ATTR_PIN = "pin"
ATTR_USER = "user"

Builder = Callable[[dict[str, Any]], "dict[str, Any] | list[dict[str, Any]]"]

_MINUTES = vol.All(vol.Coerce(float), vol.Range(min=0, max=10080))
_USER = {vol.Required(ATTR_USER): cv.string}
_OPT_USER = {vol.Optional(ATTR_USER): cv.string}


def _cmd(action: str, **keys: str) -> Builder:
    """Builder mapping service fields to a JSON command: ``_cmd("focus", minutes="minutes")``."""
    def build(data: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if ATTR_USER in data:
            payload["user"] = data[ATTR_USER]
        for target, source in keys.items():
            if source in data:
                payload[target] = data[source]
        return payload
    return build


def _volume(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if "level" in data:
        out.append({"action": "volume", "level": int(data["level"]), **({"user": data[ATTR_USER]} if ATTR_USER in data else {})})
    if "muted" in data:
        out.append({"action": "mute", "muted": bool(data["muted"]), **({"user": data[ATTR_USER]} if ATTR_USER in data else {})})
    return out


def _set_limit(data: dict[str, Any]) -> dict[str, Any]:
    return {"action": "set_limit", "user": data[ATTR_USER], "scope": data["scope"], "minutes": data.get("minutes")}


def _rule_limit(data: dict[str, Any]) -> dict[str, Any]:
    return {"action": "rule_limit", "user": data[ATTR_USER], "rule": data["rule"], "minutes": data.get("minutes")}


# name -> (fields, builder, response)
SERVICES: dict[str, tuple[dict, Builder, SupportsResponse]] = {
    "add_minutes": ({**_USER, vol.Required("minutes"): vol.All(vol.Coerce(float), vol.Range(min=1, max=1440))},
                    _cmd("add_minutes", minutes="minutes"), SupportsResponse.NONE),
    "earn": ({**_USER, vol.Required("minutes"): vol.All(vol.Coerce(float), vol.Range(min=1, max=1440)),
              vol.Optional("reason"): cv.string}, _cmd("earn", minutes="minutes", reason="reason"), SupportsResponse.NONE),
    "reward": ({**_USER, vol.Required("name"): cv.string}, _cmd("reward", name="name"), SupportsResponse.NONE),
    "set_limit": ({**_USER, vol.Required("scope"): vol.In(["today", "weekday", "weekend", "weekly"]),
                   vol.Optional("minutes"): _MINUTES}, _set_limit, SupportsResponse.NONE),
    "clear_overrides": (dict(_USER), _cmd("clear_overrides"), SupportsResponse.NONE),
    "block": (dict(_USER), _cmd("block"), SupportsResponse.NONE),
    "unblock": (dict(_USER), _cmd("unblock"), SupportsResponse.NONE),
    "focus": ({**_USER, vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
               vol.Optional("allow"): vol.All(cv.ensure_list, [cv.string])},
              _cmd("focus", minutes="minutes", allow="allow"), SupportsResponse.NONE),
    "focus_off": (dict(_USER), _cmd("focus_off"), SupportsResponse.NONE),
    "extend_bedtime": ({**_USER, vol.Optional("minutes", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=480))},
                       _cmd("extend_bedtime", minutes="minutes"), SupportsResponse.NONE),
    "rule_enabled": ({**_USER, vol.Required("rule"): cv.string, vol.Required("enabled"): cv.boolean},
                     _cmd("rule_enabled", rule="rule", enabled="enabled"), SupportsResponse.NONE),
    "rule_limit": ({**_USER, vol.Required("rule"): cv.string, vol.Optional("minutes"): _MINUTES}, _rule_limit, SupportsResponse.NONE),
    "breaks": ({**_USER, vol.Required("enabled"): cv.boolean}, _cmd("breaks", enabled="enabled"), SupportsResponse.NONE),
    "notify": ({**_OPT_USER, vol.Required("message"): cv.string, vol.Optional("title"): cv.string,
                vol.Optional("level"): vol.In(["info", "warning", "error"])},
               _cmd("notify", message="message", title="title", level="level"), SupportsResponse.NONE),
    "logoff": (dict(_OPT_USER), _cmd("logoff"), SupportsResponse.NONE),
    "lock": (dict(_OPT_USER), _cmd("lock"), SupportsResponse.NONE),
    "volume": ({**_OPT_USER, vol.Optional("level"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Optional("muted"): cv.boolean}, _volume, SupportsResponse.NONE),
    "screenshot": (dict(_USER), _cmd("screenshot"), SupportsResponse.NONE),
    "launch": ({**_OPT_USER, vol.Optional("name"): cv.string, vol.Optional("command"): cv.string},
               _cmd("launch", name="name", command="command"), SupportsResponse.NONE),
    "close_app": ({**_OPT_USER, vol.Optional("name"): cv.string, vol.Optional("process"): cv.string},
                  _cmd("close_app", name="name", process="process"), SupportsResponse.NONE),
    "power": ({vol.Required("action"): vol.In(["shutdown", "reboot", "sleep", "hibernate", "cancel_shutdown"]),
               vol.Optional("delay"): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)), vol.Optional("message"): cv.string},
              lambda d: {"action": d["action"], **{k: d[k] for k in ("delay", "message") if k in d}}, SupportsResponse.NONE),
    "wake_on_lan": ({vol.Required("mac"): cv.string, vol.Optional("broadcast"): cv.string,
                     vol.Optional("port"): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))},
                    _cmd("wol", mac="mac", broadcast="broadcast", port="port"), SupportsResponse.NONE),
    "pin": ({vol.Required(ATTR_PIN): cv.string}, lambda d: {"action": "pin"}, SupportsResponse.NONE),
    "pin_lock": ({}, _cmd("pin_lock"), SupportsResponse.NONE),
    "parent_present": ({vol.Required("present"): cv.boolean}, _cmd("parent_present", present="present"), SupportsResponse.NONE),
    "reload_config": ({}, _cmd("reload"), SupportsResponse.OPTIONAL),
    "command": ({vol.Required("data"): dict}, lambda d: dict(d["data"]), SupportsResponse.OPTIONAL),
}

_AT_LEAST_ONE = {"volume": ("level", "muted"), "launch": ("name", "command"), "close_app": ("name", "process")}


def _schema(name: str, fields: dict) -> vol.Schema:
    base = {vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]), **fields}
    if name != "pin":
        base[vol.Optional(ATTR_PIN)] = cv.string
    schema = vol.Schema(base)
    if name in _AT_LEAST_ONE:
        return vol.All(schema, cv.has_at_least_one_key(*_AT_LEAST_ONE[name]))
    return schema


def _coordinators(hass: HomeAssistant) -> dict[str, PclinkCoordinator]:
    return hass.data.get(DOMAIN, {})


def resolve_targets(hass: HomeAssistant, device_ids: Optional[list[str]]) -> list[PclinkCoordinator]:
    """Map the ``device_id`` field to coordinators; with one PC configured it may be omitted."""
    coordinators = _coordinators(hass)
    if not device_ids:
        if len(coordinators) == 1:
            return list(coordinators.values())
        raise ServiceValidationError(
            "No PC Link device is configured" if not coordinators else "Several PCs are configured: pass device_id")
    registry = dr.async_get(hass)
    found: list[PclinkCoordinator] = []
    for device_id in device_ids:
        device = registry.async_get(device_id)
        match = None
        if device is not None:
            for entry_id in device.config_entries:
                if entry_id in coordinators:
                    match = coordinators[entry_id]
                    break
        if match is None:
            raise ServiceValidationError(f"{device_id} is not a PC Link device")
        if match not in found:
            found.append(match)
    return found


def _handler(hass: HomeAssistant, name: str, builder: Builder):
    async def handle(call: ServiceCall):
        data = dict(call.data)
        targets = resolve_targets(hass, data.pop(ATTR_DEVICE_ID, None))
        pin = data.pop(ATTR_PIN, None)
        built = builder(data)
        payloads = built if isinstance(built, list) else [built]
        results: dict[str, Any] = {}
        for coordinator in targets:
            for payload in payloads:
                payload = {k: v for k, v in payload.items() if v is not None or k == "minutes"}
                if pin:
                    payload["pin"] = pin
                try:
                    results[coordinator.pc_id] = await coordinator.client.command(payload)
                except PclinkCommandError as exc:
                    raise HomeAssistantError(f"{coordinator.pc_name} refused {name}: {exc}") from exc
                except PclinkApiError as exc:
                    raise HomeAssistantError(f"{coordinator.pc_name} could not be reached: {exc}") from exc
            await coordinator.async_request_refresh()
        if call.return_response:
            return {"results": results}
        return None
    return handle


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "add_minutes"):
        return
    for name, (fields, builder, response) in SERVICES.items():
        hass.services.async_register(DOMAIN, name, _handler(hass, name, builder), schema=_schema(name, fields),
                                     supports_response=response)


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    for name in SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
