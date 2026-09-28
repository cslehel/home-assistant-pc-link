"""Diagnostics download (token redacted)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN, DOMAIN
from .coordinator import PclinkCoordinator

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: PclinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "health": coordinator.health,
        "websocket_connected": coordinator.ws_connected,
        "last_update_success": coordinator.last_update_success,
        "catalog": coordinator.catalog,
        "pc": coordinator.pc,
        "users": coordinator.users,
    }
