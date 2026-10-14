"""Diagnostics download (token redacted)."""
from __future__ import annotations

import re
from typing import Any, Optional

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_FINGERPRINT, CONF_TOKEN, DOMAIN
from .coordinator import PclinkCoordinator

TO_REDACT = {CONF_TOKEN, CONF_HOST, CONF_FINGERPRINT}
REDACTED = "**REDACTED**"

# Diagnostics are meant to be attached to public issues.  The PC and user payloads are
# therefore copied by allow-list: structure, numbers, booleans and dates stay; a text stays
# only under a key listed here, whose values are states and settings out of a fixed set
# ("active", "logoff", a version) - never a name, an address, a program, a website or
# something a person typed.  A field added to the payload later is redacted until it is
# listed, which is the safe way round.
PAYLOAD_TEXT_ALLOWED = frozenset({
    # states
    "status", "state", "presence", "activity", "limiting_factor", "service_status", "source", "kind", "level",
    "tamper_reason",
    # settings with fixed choices
    "action", "rule_action", "block_method", "format", "mode", "method", "hw_backend", "gpu_backend",
    # versions and hashes
    "version", "python", "pclink_installed_version", "pclink_latest_version", "catalog_hash",
    # dates and times
    "date", "week_start", "started", "last_boot", "last_update_check", "last_good_tick", "tamper_at",
    "bedtime", "wake", "until", "from", "to", "ts", "at", "expires", "since",
})
# Mappings whose *keys* are names (programs, websites, rules, PCs): the keys are numbered.
PAYLOAD_NAME_KEYED = frozenset({"top_apps", "top_games", "top_sites", "rules", "apps", "games", "sites", "per_pc", "pcs"})
_PLAIN_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def redact_payload(value: Any, key: str = "", named: bool = False) -> Any:
    """A copy of a PC / user payload (or a part of it) that is safe to publish; see above."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (name, item) in enumerate(value.items(), start=1):
            name = str(name)
            # a key that does not look like a field name is data as well ("chrome.exe", "youtube.com")
            shown = f"item_{index}" if named or not _PLAIN_KEY.match(name) else name
            out[shown] = redact_payload(item, name, name in PAYLOAD_NAME_KEYED)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_payload(item, key) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and (value == "" or key in PAYLOAD_TEXT_ALLOWED):
        return value
    return REDACTED


def _catalog_summary(catalog: dict[str, Any]) -> dict[str, Any]:
    """The catalog without names: PC entities by key, per-user entities reduced to their shape."""
    by_platform: dict[str, int] = {}
    pc_entities: list[dict[str, Any]] = []
    user_shapes: set[tuple[str, str, str, str]] = set()
    for spec in catalog.get("entities") or []:
        platform = str(spec.get("platform"))
        by_platform[platform] = by_platform.get(platform, 0) + 1
        if spec.get("source") == "user":
            # keys and names embed the user name; rule / reward entries embed their names in the value path
            value = str(spec.get("value") or "")
            if value.startswith("rules."):
                value = "rules.<rule>." + value.split(".", 2)[-1] if value.count(".") >= 2 else "rules.<rule>"
            user_shapes.add((platform, value, str(spec.get("device_class") or ""), str(spec.get("unit") or "")))
        elif str(spec.get("key") or "").startswith(("launch_", "close_", "cmd_")):
            continue  # the key is the name of a configured app / command; counted above only
        else:
            pc_entities.append({k: v for k, v in spec.items() if k not in ("name", "unique_id")})
    return {
        "version": catalog.get("version"),
        "hash": catalog.get("hash"),
        "device": async_redact_data(dict(catalog.get("device") or {}), {"name", "identifiers", "suggested_area"}),
        "user_count": len(catalog.get("users") or {}),
        "entities_by_platform": by_platform,
        "pc_entities": pc_entities,
        "user_entity_shapes": [dict(zip(("platform", "value", "device_class", "unit"), shape)) for shape in sorted(user_shapes)],
        "triggers": {kind: sorted(types) for kind, types in (catalog.get("triggers") or {}).items() if isinstance(types, (dict, list))},
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    data: dict[str, Any] = {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_state": str(entry.state),
    }
    coordinator: Optional[PclinkCoordinator] = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:  # the PC was not reachable at setup: there is nothing more to report
        data["loaded"] = False
        return data
    data.update({
        "loaded": True,
        "health": redact_payload(dict(coordinator.health)),
        "websocket_connected": coordinator.ws_connected,
        "online": coordinator.online,
        "wrong_pc_answers": coordinator.wrong_pc is not None,
        "last_update_success": coordinator.last_update_success,
        "catalog": _catalog_summary(coordinator.catalog),
        "pc": redact_payload(dict(coordinator.pc)),
        # user names are the keys: replace them by position
        "users": {f"user_{index}": redact_payload(dict(payload)) if isinstance(payload, dict) else None
                  for index, payload in enumerate(coordinator.users.values(), start=1)},
    })
    return data
