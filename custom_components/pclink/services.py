"""Home Assistant actions (services) for PC Link: ``pclink.add_minutes``, ``pclink.focus``, ...

Every action targets one PC (``device_id``; optional when only one PC is
configured) and is sent as the JSON command the PC's local API understands.
Refusals - typically "parent pin required" - surface as errors in the UI.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .api import PclinkApiError, PclinkAuthError, PclinkCommandError, PclinkThrottledError, PclinkWrongDeviceError
from .const import DOMAIN, ROLLUP_DEVICE_ID, device_entry_ids
from .coordinator import PclinkCoordinator

_LOGGER = logging.getLogger(__name__)

ATTR_DEVICE_ID = "device_id"
ATTR_ENTITY_ID = "entity_id"
ATTR_AREA_ID = "area_id"
ATTR_FLOOR_ID = "floor_id"
ATTR_LABEL_ID = "label_id"
ATTR_PIN = "pin"
ATTR_USER = "user"
# What says where an action goes: the ``device_id`` field, or a ``target:`` with devices,
# entities, areas, floors or labels (Home Assistant puts those into the call's data under
# these names; floors and labels exist from Home Assistant 2024.4 on).
_TARGET_KEYS = (ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_AREA_ID, ATTR_FLOOR_ID, ATTR_LABEL_ID)

# Actions that run programs or code on the PC, or change what it runs: only Home Assistant
# administrators may call them (automations and scripts, which run without a user, still can).
# Granting time is not among them - the PC guards that with the parent PIN.
ADMIN_ACTIONS = frozenset({"command", "launch", "reload_config"})

# Actions the "PC Link (all PCs)" device can be the target of: they are sent to every PC.
# The others do one thing on one PC - minutes granted on every PC would be granted several
# times over, a time request waits on the PC it was made on - and are refused for it.
ALL_PCS_ACTIONS = frozenset({
    "block", "unblock", "focus", "focus_off", "extend_bedtime", "breaks", "rule_enabled", "set_rule", "remove_rule",
    "block_item", "unblock_item", "notify", "logoff", "lock", "display", "volume", "close_app", "power",
    "pin", "pin_lock", "parent_present", "usage_report", "reload_config", "set_profile",
})

Builder = Callable[[dict[str, Any]], "dict[str, Any] | list[dict[str, Any]]"]

_MINUTES = vol.All(vol.Coerce(float), vol.Range(min=0, max=10080))
# A user name is never empty: the PC reads an empty ``user`` as "every session", so a
# template that renders to "" must not turn "log off kevin" into "log off everybody".
_USER_NAME = vol.All(cv.string, vol.Strip, vol.Length(min=1, msg="user must not be empty; omit the field to address every session"))
_USER = {vol.Required(ATTR_USER): _USER_NAME}
_OPT_USER = {vol.Optional(ATTR_USER): _USER_NAME}


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


UNLIMITED = "unlimited"
# minutes of a limit: a number, or the text ``unlimited``.  No minutes at all is something
# else on the PC: the override is dropped and the configured limit applies again.
_LIMIT_MINUTES = vol.Any(_MINUTES, vol.All(cv.string, vol.Strip, vol.Lower, vol.In([UNLIMITED])))


def _limit_minutes(minutes: Any, scope: str) -> Any:
    """``unlimited`` as the PC's "no limit": 0 for a weekly limit, any negative number for the others."""
    if minutes == UNLIMITED:
        return 0 if scope == "weekly" else -1
    return minutes


def _set_limit(data: dict[str, Any]) -> dict[str, Any]:
    return {"action": "set_limit", "user": data[ATTR_USER], "scope": data["scope"],
            "minutes": _limit_minutes(data.get("minutes"), data["scope"])}


def _rule_limit(data: dict[str, Any]) -> dict[str, Any]:
    return {"action": "rule_limit", "user": data[ATTR_USER], "rule": data["rule"],
            "minutes": _limit_minutes(data.get("minutes"), str(data.get("scope") or "today")),
            **({"scope": data["scope"]} if "scope" in data else {})}


# what ``set_rule`` may change: the keys a rule has in the PC's config.yaml
_RULE_KEYS = ("apps", "sites", "categories", "daily_limit_minutes", "weekly_limit_minutes", "allowed_hours",
              "blocked", "rule_action", "message")
_NAMES = vol.All(cv.ensure_list, [cv.string])
# minutes as a number, as text ("90", "1.5h", "unlimited") or per day ({weekdays: 30, weekend: 60})
_RULE_MINUTES = vol.Any(vol.All(vol.Coerce(float), vol.Range(min=0, max=10080)), cv.string, dict)


def _set_rule(data: dict[str, Any]) -> dict[str, Any]:
    return {"action": "rule_set", "user": data[ATTR_USER], "rule": data["rule"], **{k: data[k] for k in _RULE_KEYS if k in data}}


# name -> (fields, builder, response)
SERVICES: dict[str, tuple[dict, Builder, SupportsResponse]] = {
    "add_minutes": ({**_USER, vol.Required("minutes"): vol.All(vol.Coerce(float), vol.Range(min=1, max=1440))},
                    _cmd("add_minutes", minutes="minutes"), SupportsResponse.NONE),
    "earn": ({**_USER, vol.Required("minutes"): vol.All(vol.Coerce(float), vol.Range(min=1, max=1440)),
              vol.Optional("reason"): cv.string}, _cmd("earn", minutes="minutes", reason="reason"), SupportsResponse.NONE),
    "reward": ({**_USER, vol.Required("name"): cv.string}, _cmd("reward", name="name"), SupportsResponse.NONE),
    "set_limit": ({**_USER, vol.Required("scope"): vol.In(["today", "weekday", "weekend", "weekly"]),
                   vol.Optional("minutes"): _LIMIT_MINUTES}, _set_limit, SupportsResponse.NONE),
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
    "rule_limit": ({**_USER, vol.Required("rule"): cv.string, vol.Optional("minutes"): _LIMIT_MINUTES,
                    vol.Optional("scope"): vol.In(["today", "weekly"])}, _rule_limit, SupportsResponse.NONE),
    "set_rule": ({**_USER, vol.Required("rule"): vol.All(cv.string, vol.Strip, vol.Length(min=1, max=80)),
                  vol.Optional("apps"): _NAMES, vol.Optional("sites"): _NAMES, vol.Optional("categories"): _NAMES,
                  vol.Optional("daily_limit_minutes"): _RULE_MINUTES, vol.Optional("weekly_limit_minutes"): _RULE_MINUTES,
                  vol.Optional("allowed_hours"): vol.Any(cv.string, dict, [cv.string]),
                  vol.Optional("blocked"): cv.boolean, vol.Optional("rule_action"): vol.In(["close", "warn"]),
                  vol.Optional("message"): cv.string}, _set_rule, SupportsResponse.OPTIONAL),
    "remove_rule": ({**_USER, vol.Required("rule"): cv.string}, _cmd("rule_remove", rule="rule"), SupportsResponse.OPTIONAL),
    "display": ({**_OPT_USER, vol.Required("enabled"): cv.boolean}, _cmd("display", on="enabled"), SupportsResponse.NONE),
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
    "block_item": ({**_USER, vol.Required("item"): vol.All(cv.string, vol.Length(min=1, max=200))},
                   _cmd("block_item", item="item"), SupportsResponse.OPTIONAL),
    "unblock_item": ({**_USER, vol.Required("item"): vol.All(cv.string, vol.Length(min=1, max=200))},
                     _cmd("unblock_item", item="item"), SupportsResponse.OPTIONAL),
    "usage_report": ({**_USER, vol.Optional("hours"): vol.All(vol.Coerce(float), vol.Range(min=1, max=336))},
                     _cmd("usage_report", hours="hours"), SupportsResponse.OPTIONAL),
    "answer_time_request": ({**_USER, vol.Required("approve"): cv.boolean,
                             vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440))},
                            _cmd("time_request_answer", approve="approve", minutes="minutes"), SupportsResponse.OPTIONAL),
    # without a user: every managed user that has that profile ("normal" = no profile)
    "set_profile": ({**_OPT_USER, vol.Required("profile"): vol.All(cv.string, vol.Strip, vol.Length(min=1, max=32))},
                    _cmd("set_profile", profile="profile"), SupportsResponse.OPTIONAL),
    "reload_config": ({}, _cmd("reload"), SupportsResponse.OPTIONAL),
    "command": ({vol.Required("data"): dict}, lambda d: dict(d["data"]), SupportsResponse.OPTIONAL),
}

_AT_LEAST_ONE = {"volume": ("level", "muted"), "launch": ("name", "command"), "close_app": ("name", "process")}


def _schema(name: str, fields: dict) -> vol.Schema:
    base = {**{vol.Optional(key): vol.All(cv.ensure_list, [cv.string]) for key in _TARGET_KEYS}, **fields}
    if name != "pin":
        base[vol.Optional(ATTR_PIN)] = cv.string
    schema = vol.Schema(base)
    if name in _AT_LEAST_ONE:
        return vol.All(schema, cv.has_at_least_one_key(*_AT_LEAST_ONE[name]))
    return schema


def _coordinators(hass: HomeAssistant) -> dict[str, PclinkCoordinator]:
    return hass.data.get(DOMAIN, {})


def _floor_areas(hass: HomeAssistant, floor_ids: list[str]) -> list[str]:
    """The areas on these floors (none before Home Assistant 2024.4, which has no floors)."""
    areas = ar.async_get(hass).async_list_areas()
    return [area.id for area in areas if getattr(area, "floor_id", None) in floor_ids]


def _label_devices(hass: HomeAssistant, label_ids: list[str]) -> list[str]:
    """PC Link devices with one of these labels, of an entity with one, or in an area with one."""
    wanted = set(label_ids)
    ours = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    found: list[str] = []
    for entry_id in ours:
        found += [device.id for device in dr.async_entries_for_config_entry(devices, entry_id)
                  if wanted & set(getattr(device, "labels", ()) or ())]
        found += [reg_entry.device_id for reg_entry in er.async_entries_for_config_entry(entities, entry_id)
                  if reg_entry.device_id and wanted & set(getattr(reg_entry, "labels", ()) or ())]
    areas = [area.id for area in ar.async_get(hass).async_list_areas() if wanted & set(getattr(area, "labels", ()) or ())]
    for area_id in areas:
        found += [device.id for device in dr.async_entries_for_area(devices, area_id) if ours & set(device_entry_ids(device))]
    return found


def _target_devices(hass: HomeAssistant, device_ids: Optional[list[str]], entity_ids: Optional[list[str]],
                    area_ids: Optional[list[str]], floor_ids: Optional[list[str]] = None,
                    label_ids: Optional[list[str]] = None) -> list[str]:
    """Device ids named by the call: directly, as the device of a PC Link entity, as the
    PC Link devices of an area or of the areas of a floor, or by a label."""
    devices = list(device_ids or [])
    if floor_ids:
        floor_areas = _floor_areas(hass, floor_ids)
        ours = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}
        registry = dr.async_get(hass)
        found = [device.id for area_id in floor_areas for device in dr.async_entries_for_area(registry, area_id)
                 if ours & set(device_entry_ids(device))]
        if not found:
            raise ServiceValidationError(f"There is no PC Link device on the floor {', '.join(floor_ids)}")
        devices += found
    if label_ids:
        found = _label_devices(hass, label_ids)
        if not found:
            raise ServiceValidationError(f"There is no PC Link device with the label {', '.join(label_ids)}")
        devices += found
    if entity_ids:
        registry = er.async_get(hass)
        for entity_id in entity_ids:
            reg_entry = registry.async_get(entity_id)
            if reg_entry is None or reg_entry.platform != DOMAIN or not reg_entry.device_id:
                raise ServiceValidationError(f"{entity_id} is not a PC Link entity")
            devices.append(reg_entry.device_id)
    if area_ids:
        registry = dr.async_get(hass)
        ours = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}
        for area_id in area_ids:
            found = [device.id for device in dr.async_entries_for_area(registry, area_id) if ours & set(device_entry_ids(device))]
            if not found:
                raise ServiceValidationError(f"There is no PC Link device in the area {area_id}")
            devices += found
    return list(dict.fromkeys(devices))


def resolve_targets(hass: HomeAssistant, device_ids: Optional[list[str]], action: Optional[str] = None,
                    entity_ids: Optional[list[str]] = None, area_ids: Optional[list[str]] = None,
                    floor_ids: Optional[list[str]] = None, label_ids: Optional[list[str]] = None) -> list[PclinkCoordinator]:
    """Map the ``device_id`` field (or a ``target:``) to coordinators; with one PC configured
    it may be omitted.  The "PC Link (all PCs)" device stands for every PC that is loaded,
    for the actions that can be sent to all of them (``ALL_PCS_ACTIONS``)."""
    coordinators = _coordinators(hass)
    entries = {entry.entry_id: entry for entry in hass.config_entries.async_entries(DOMAIN)
               if entry.source != "ignore" and not entry.disabled_by}
    device_ids = _target_devices(hass, device_ids, entity_ids, area_ids, floor_ids, label_ids)
    if not device_ids:
        # "the only PC" is decided on what is configured, not on what happens to be
        # loaded: with two PCs of which one is off, the other must not be picked silently.
        if len(entries) > 1:
            raise ServiceValidationError("Several PCs are configured: pass device_id")
        if len(coordinators) == 1:
            return list(coordinators.values())
        if entries:
            raise HomeAssistantError(f"{next(iter(entries.values())).title} is not available (the PC could not be reached)")
        raise ServiceValidationError("No PC Link device is configured")
    registry = dr.async_get(hass)
    found: list[PclinkCoordinator] = []
    for device_id in device_ids:
        device = registry.async_get(device_id)
        match = None
        not_loaded = None
        if device is not None and (DOMAIN, ROLLUP_DEVICE_ID) in device.identifiers:
            # The roll-up device belongs to one PC's entry, but it is not that PC.
            if action not in ALL_PCS_ACTIONS:
                raise ServiceValidationError(
                    f"pclink.{action} does one thing on one PC and cannot be sent to all PCs at once: choose the PC")
            if not coordinators:
                raise HomeAssistantError("No PC is available (none could be reached)")
            found += [coordinator for coordinator in coordinators.values() if coordinator not in found]
            continue
        if device is not None:
            for entry_id in device_entry_ids(device):
                if entry_id in coordinators:
                    match = coordinators[entry_id]
                    break
                if entry_id in entries:
                    not_loaded = entries[entry_id]
        if match is None:
            if not_loaded is not None:
                raise HomeAssistantError(f"{not_loaded.title} is not available (the PC could not be reached)")
            raise ServiceValidationError(f"{device_id} is not a PC Link device")
        if match not in found:
            found.append(match)
    return found


def _handler(hass: HomeAssistant, name: str, builder: Builder):
    async def handle(call: ServiceCall):
        data = dict(call.data)
        targets = resolve_targets(hass, data.pop(ATTR_DEVICE_ID, None), name,
                                  data.pop(ATTR_ENTITY_ID, None), data.pop(ATTR_AREA_ID, None),
                                  data.pop(ATTR_FLOOR_ID, None), data.pop(ATTR_LABEL_ID, None))
        pin = data.pop(ATTR_PIN, None)
        built = builder(data)
        payloads = built if isinstance(built, list) else [built]
        results: dict[str, Any] = {}
        errors: list[str] = []
        failed: dict[str, str] = {}      # pc id -> what went wrong there
        first_error: Optional[Exception] = None
        throttled: dict[str, HomeAssistantError] = {}   # pc id -> "the PC asked to wait N s" (translated)
        # Every PC is tried: one that refuses or is off must not keep the others from getting the command.
        for coordinator in targets:
            answers: list[dict[str, Any]] = []
            before = len(errors)
            try:
                for payload in payloads:
                    payload = {k: v for k, v in payload.items() if v is not None or k == "minutes"}
                    if pin:
                        payload["pin"] = pin
                    answers.append(await coordinator.async_command(payload))
            except PclinkThrottledError as exc:
                # refused with a retry_after, or held back because the PC asked to wait (nothing sent)
                err = coordinator.throttled_error(exc)
                throttled[coordinator.pc_id] = err
                errors.append(str(err))
                first_error = first_error or exc
            except PclinkCommandError as exc:
                errors.append(f"{coordinator.pc_name} refused {name}: {exc}")
                first_error = first_error or exc
            except PclinkWrongDeviceError as exc:
                errors.append(f"{coordinator.entry.title}: {exc}")
                first_error = first_error or exc
            except PclinkAuthError as exc:
                coordinator.async_start_reauth("the API token was rejected for a command")
                errors.append(f"{coordinator.pc_name} rejected the API token; re-authentication required")
                first_error = first_error or exc
            except PclinkApiError as exc:
                errors.append(f"{coordinator.pc_name} could not be reached: {exc}")
                first_error = first_error or exc
            if len(errors) > before:
                failed[coordinator.pc_id] = errors[-1]
            if answers:
                # one command per PC for every action but ``volume`` (level + mute): keep the shape, lose nothing
                results[coordinator.pc_id] = answers[0] if len(payloads) == 1 else {"ok": len(answers) == len(payloads), "results": answers}
                await coordinator.async_request_state()
        if errors and not (call.return_response and len(failed) < len(targets)):
            if throttled and len(throttled) == len(errors):
                # every failure is a PC that asked to wait: say so, translated (the longest wait)
                if len(throttled) == 1:
                    raise next(iter(throttled.values())) from first_error
                placeholders = {"pc": ", ".join(e.translation_placeholders["pc"] for e in throttled.values()),
                                "seconds": str(max(int(e.translation_placeholders["seconds"]) for e in throttled.values()))}
                raise HomeAssistantError("; ".join(errors), translation_domain=DOMAIN, translation_key="throttled",
                                         translation_placeholders=placeholders) from first_error
            # All PCs are reported together: the caller sees which took the command and which did not.
            raise HomeAssistantError("; ".join(errors)) from first_error
        if call.return_response:
            # Some PCs took it: their answers are not thrown away for the others' failure; what
            # went wrong where is part of the response instead (and how long a PC asked to wait).
            waits = {pc: int(err.translation_placeholders["seconds"]) for pc, err in throttled.items()}
            return {"results": results, **({"errors": failed} if failed else {}),
                    **({"retry_after": waits} if waits else {})}
        return None
    return handle


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Called from ``async_setup``: the actions exist independently of any config entry."""
    if hass.services.has_service(DOMAIN, "add_minutes"):
        return
    admin_takes_response = "supports_response" in inspect.signature(async_register_admin_service).parameters
    for name, (fields, builder, response) in SERVICES.items():
        handler = _handler(hass, name, builder)
        if name in ADMIN_ACTIONS and admin_takes_response:
            async_register_admin_service(hass, DOMAIN, name, handler, schema=_schema(name, fields), supports_response=response)
        else:
            if name in ADMIN_ACTIONS:  # a release whose helper cannot pass a response on: the same check here
                handler = _admin_only(hass, handler)
            hass.services.async_register(DOMAIN, name, handler, schema=_schema(name, fields), supports_response=response)


def _admin_only(hass: HomeAssistant, handler: Callable[[ServiceCall], Any]) -> Callable[[ServiceCall], Any]:
    """What ``async_register_admin_service`` does: a call by a user who is not an
    administrator is refused; one without a user (an automation, a script) is not."""
    async def handle(call: ServiceCall):
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context)
            if not user.is_admin:
                raise Unauthorized(context=call.context)
        return await handler(call)
    return handle


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    for name in SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
