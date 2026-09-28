"""Config flow: add a PC by address + API token, or accept one discovered via mDNS."""
from __future__ import annotations

import logging
from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PclinkApiError, PclinkAuthError, PclinkClient, PclinkConnectionError, PclinkOutdatedError
from .const import CONF_TOKEN, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
USER_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
    vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
})
TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR})


async def validate_connection(hass: HomeAssistant, host: str, port: int, token: str) -> dict[str, Any]:
    """Return the PC's health document or raise a PclinkApiError subclass."""
    client = PclinkClient(async_get_clientsession(hass), host, port, token)
    health = await client.health()
    await client.entities()  # proves the service is new enough for the integration
    return health


def _error_key(exc: Exception) -> str:
    if isinstance(exc, PclinkAuthError):
        return "invalid_auth"
    if isinstance(exc, PclinkOutdatedError):
        return "outdated"
    if isinstance(exc, PclinkConnectionError):
        return "cannot_connect"
    return "unknown"


class PclinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    # -- manual ---------------------------------------------------------------------------
    async def async_step_user(self, user_input: Optional[dict[str, Any]] = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port, token = user_input[CONF_HOST].strip(), int(user_input[CONF_PORT]), user_input[CONF_TOKEN]
            try:
                health = await validate_connection(self.hass, host, port, token)
            except PclinkApiError as exc:
                errors["base"] = _error_key(exc)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error validating %s", host)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(str(health["device"]))
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token})
                return self.async_create_entry(title=str(health.get("name") or host),
                                               data={CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token})
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
                                    errors=errors)

    # -- mDNS discovery -------------------------------------------------------------------
    async def async_step_zeroconf(self, discovery_info: Any):
        """``discovery_info`` is Home Assistant's ZeroconfServiceInfo (host, port, name, properties)."""
        props = {str(k): str(v) for k, v in (discovery_info.properties or {}).items()}
        pc_id = props.get("id")
        if not pc_id:
            return self.async_abort(reason="no_id")
        host = discovery_info.host
        port = int(discovery_info.port or DEFAULT_PORT)
        name = props.get("name") or discovery_info.name.split(".")[0]
        await self.async_set_unique_id(pc_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        self._discovered = {CONF_HOST: host, CONF_PORT: port, "name": name}
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: Optional[dict[str, Any]] = None):
        errors: dict[str, str] = {}
        host, port, name = self._discovered[CONF_HOST], self._discovered[CONF_PORT], self._discovered["name"]
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            try:
                health = await validate_connection(self.hass, host, port, token)
            except PclinkApiError as exc:
                errors["base"] = _error_key(exc)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error validating %s", host)
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=str(health.get("name") or name),
                                               data={CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token})
        return self.async_show_form(step_id="zeroconf_confirm", data_schema=TOKEN_SCHEMA, errors=errors,
                                    description_placeholders={"name": name, "host": f"{host}:{port}"})

    # -- token changed --------------------------------------------------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: Optional[dict[str, Any]] = None):
        entry = self._current_entry()
        errors: dict[str, str] = {}
        if user_input is not None and entry is not None:
            token = user_input[CONF_TOKEN]
            try:
                await validate_connection(self.hass, entry.data[CONF_HOST], entry.data[CONF_PORT], token)
            except PclinkApiError as exc:
                errors["base"] = _error_key(exc)
            else:
                return self.async_update_reload_and_abort(entry, data={**entry.data, CONF_TOKEN: token},
                                                          reason="reauth_successful")
        return self.async_show_form(step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors,
                                    description_placeholders={"name": entry.title if entry else ""})

    # -- address changed ------------------------------------------------------------------
    async def async_step_reconfigure(self, user_input: Optional[dict[str, Any]] = None):
        entry = self._current_entry()
        if entry is None:
            return self.async_abort(reason="unknown")
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port, token = user_input[CONF_HOST].strip(), int(user_input[CONF_PORT]), user_input[CONF_TOKEN]
            try:
                health = await validate_connection(self.hass, host, port, token)
            except PclinkApiError as exc:
                errors["base"] = _error_key(exc)
            else:
                if entry.unique_id and str(health.get("device")) != entry.unique_id:
                    errors["base"] = "wrong_device"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data={CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token}, reason="reconfigure_successful")
        return self.async_show_form(step_id="reconfigure",
                                    data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or dict(entry.data)),
                                    errors=errors, description_placeholders={"name": entry.title})

    def _current_entry(self) -> Optional[config_entries.ConfigEntry]:
        entry_id = self.context.get("entry_id")
        return self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
