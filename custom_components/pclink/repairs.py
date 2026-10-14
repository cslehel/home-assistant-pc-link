"""Repairs: the two situations in which a PC's entry points at the wrong place.

* ``wrong_pc`` - a different PC answers at the configured address (coordinator.py);
* ``pc_moved`` - the PC announced itself at another address while it could not be
  reached at the configured one (config_flow.py).

Both are fixed the same way, by what "Reconfigure" does: the user confirms or types
the address (pre-filled with the announced one), the PC there has to be the one the
entry was made for and accept the token, and the entry is updated and reloaded.

Only the host and port are taken from an announcement; HTTPS and the pinned certificate
are always the entry's own, and for an address other than the configured one the token
has to be typed again unless the entry's pinned certificate stays (``keeps_token``).
"""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .config_flow import RECONFIGURE_SCHEMA, address_placeholders, async_validate_new_address
from .const import CONF_FINGERPRINT, CONF_TLS


class AddressRepairFlow(RepairsFlow):
    """Point an entry at the address its PC is at now."""

    def __init__(self, entry_id: str, suggested: dict[str, Any]) -> None:
        self._entry_id = entry_id
        self._suggested = suggested

    async def async_step_init(self, user_input: Optional[dict[str, Any]] = None):
        return await self.async_step_address()

    async def async_step_address(self, user_input: Optional[dict[str, Any]] = None):
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="unknown")
        errors: dict[str, str] = {}
        if user_input is not None:
            data, error = await async_validate_new_address(self.hass, entry, user_input)
            if data is None:
                errors["base"] = error or "unknown"
            else:
                self.hass.config_entries.async_update_entry(entry, data=data)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data={})
        shown = {key: value for key, value in (user_input or self._suggested).items()
                 if key in (CONF_HOST, CONF_PORT, CONF_TLS, CONF_FINGERPRINT) and value is not None}
        return self.async_show_form(step_id="address", data_schema=self.add_suggested_values_to_schema(RECONFIGURE_SCHEMA, shown),
                                    errors=errors, description_placeholders=address_placeholders(entry))


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: Optional[dict[str, Any]]) -> RepairsFlow:
    if not data or not data.get("entry_id"):
        return ConfirmRepairFlow()
    entry = hass.config_entries.async_get_entry(str(data["entry_id"]))
    # host and port from the issue (an announcement, or the address in use); never HTTPS or a
    # fingerprint - an issue raised by an older version may still carry them
    suggested = {**(dict(entry.data) if entry is not None else {}),
                 **{key: data[key] for key in (CONF_HOST, CONF_PORT) if data.get(key) is not None}}
    return AddressRepairFlow(str(data["entry_id"]), suggested)
