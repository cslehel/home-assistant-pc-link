"""Config flow: add a PC by address + API token, or accept one discovered via mDNS."""
from __future__ import annotations

import logging
from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    PclinkApiError,
    PclinkAuthError,
    PclinkBusyError,
    PclinkClient,
    PclinkConnectionError,
    PclinkOutdatedError,
    PclinkThrottledError,
    PclinkTlsError,
    normalize_fingerprint,
    split_address,
)
from .const import CONF_FINGERPRINT, CONF_TLS, CONF_TOKEN, DEFAULT_PORT, DOMAIN, ISSUE_MOVED, issue_id

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
_PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
# HTTPS is optional: a PC with api.tls_cert serves it, and the fingerprint of its (usually
# self-signed) certificate is what `pclink make-cert` prints.
_TLS_FIELDS = {vol.Optional(CONF_TLS, default=False): bool, vol.Optional(CONF_FINGERPRINT): str}
USER_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_PORT, default=DEFAULT_PORT): _PORT,
    vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
    **_TLS_FIELDS,
})
TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR})
# Reconfigure: the stored token is never sent back to the browser; an empty field keeps it.
RECONFIGURE_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_PORT, default=DEFAULT_PORT): _PORT,
    vol.Optional(CONF_TOKEN): TOKEN_SELECTOR,
    **_TLS_FIELDS,
})


def read_address(user_input: dict[str, Any]) -> dict[str, Any]:
    """The address fields of a form as entry data: ``{host, port, tls, fingerprint}``.

    The host may be pasted with a scheme, a port or a path (``https://pc.local:47392/``);
    what it says wins over the separate fields.  Raises ValueError with the key of the
    error to show (``invalid_host`` / ``invalid_fingerprint``)."""
    try:
        host, port, tls = split_address(user_input[CONF_HOST])
    except ValueError as exc:
        raise ValueError("invalid_host") from exc
    try:
        fingerprint = normalize_fingerprint(user_input.get(CONF_FINGERPRINT))
    except ValueError as exc:
        raise ValueError("invalid_fingerprint") from exc
    tls = bool(fingerprint) or (bool(user_input.get(CONF_TLS)) if tls is None else tls)
    data: dict[str, Any] = {CONF_HOST: host, CONF_PORT: port or int(user_input.get(CONF_PORT) or DEFAULT_PORT)}
    if tls:  # an entry without HTTPS keeps the three keys it always had
        data[CONF_TLS] = True
        if fingerprint:
            data[CONF_FINGERPRINT] = fingerprint
    return data


async def validate_connection(hass: HomeAssistant, host: str, port: int, token: str,
                              tls: bool = False, fingerprint: Optional[str] = None) -> dict[str, Any]:
    """Return the PC's health document (always with a ``device`` id) or raise a PclinkApiError subclass."""
    if tls or fingerprint:
        client = PclinkClient(async_get_clientsession(hass), host, port, token, True, fingerprint)
    else:
        client = PclinkClient(async_get_clientsession(hass), host, port, token)
    health = await client.health()
    if not health.get("device"):
        raise PclinkOutdatedError("the PC did not report its device id")
    await client.entities()  # proves the service is new enough for the integration
    # Reads need no token when Home Assistant reaches the PC over loopback, so the two
    # calls above would accept any token there; this one always needs the right one.
    await client.check_write_auth()
    return health


async def try_connection(hass: HomeAssistant, address: dict[str, Any], token: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(health, None)`` or ``(None, error key)``; used by every step so they all handle errors alike."""
    try:
        return await validate_connection(hass, address[CONF_HOST], address[CONF_PORT], token,
                                         bool(address.get(CONF_TLS)), address.get(CONF_FINGERPRINT)), None
    except PclinkApiError as exc:
        return None, _error_key(exc)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("unexpected error validating %s", address.get(CONF_HOST))
        return None, "unknown"


def _error_key(exc: Exception) -> str:
    if isinstance(exc, PclinkAuthError):
        return "invalid_auth"
    if isinstance(exc, PclinkBusyError):
        return "busy"
    if isinstance(exc, PclinkThrottledError):
        return "too_many_attempts"
    if isinstance(exc, PclinkOutdatedError):
        return "outdated"
    if isinstance(exc, PclinkTlsError):
        return "tls_error"
    if isinstance(exc, PclinkConnectionError):
        return "cannot_connect"
    return "unknown"


def _shown(data: dict[str, Any]) -> dict[str, Any]:
    """What of an address is offered back in a form (never the token: it would travel to the browser)."""
    return {key: value for key, value in data.items() if key in (CONF_HOST, CONF_PORT, CONF_TLS, CONF_FINGERPRINT)}


def describe_security(tls: Any, fingerprint: Optional[str]) -> str:
    """How the connection to a PC is protected, for a form's description: the user checks it
    against what ``pclink setup-api`` / ``make-cert`` printed on the PC."""
    if fingerprint:
        return f"HTTPS, only the certificate with the SHA-256 fingerprint {fingerprint}"
    if tls:
        return "HTTPS, the certificate checked against the authorities Home Assistant trusts (no fingerprint pinned)"
    return "HTTP, not encrypted (no certificate)"


class PclinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    # -- manual ---------------------------------------------------------------------------
    async def async_step_user(self, user_input: Optional[dict[str, Any]] = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            health: Optional[dict[str, Any]] = None
            try:
                address = read_address(user_input)
            except ValueError as exc:
                errors["base"] = str(exc)
            else:
                health, error = await try_connection(self.hass, address, token)
                if health is None:
                    errors["base"] = error or "unknown"
            if health is not None:
                # The address was typed by the user and the PC behind it proved its identity
                # (it accepted the token), so an existing entry for that PC may follow it.
                # This is also how an address is changed where there is no "Reconfigure" yet
                # (Home Assistant before 2024.4).
                await self.async_set_unique_id(str(health["device"]))
                # Every address key, so that a PC added again over plain HTTP does not keep the
                # HTTPS switch and fingerprint the existing entry had (``updates`` is merged in).
                self._abort_if_unique_id_configured(
                    updates={CONF_TLS: False, CONF_FINGERPRINT: None, **address, CONF_TOKEN: token})
                return self.async_create_entry(title=str(health.get("name") or address[CONF_HOST]),
                                               data={**address, CONF_TOKEN: token})
        # offered back after an error without the token: it would travel to the browser
        shown = _shown(user_input) if user_input is not None else None
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, shown),
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
        # a PC that serves HTTPS says so, and which certificate to expect (tls=1, fp=<sha256>)
        try:
            fingerprint = normalize_fingerprint(props.get("fp"))
        except ValueError:
            fingerprint = None
        tls = bool(fingerprint) or props.get("tls", "").lower() in ("1", "true", "yes")
        await self.async_set_unique_id(pc_id)
        # An announcement is unauthenticated: anything on the LAN can claim to be a known
        # PC, and a PC with several network adapters announces addresses Home Assistant
        # cannot reach.  So a configured PC is NEVER repointed from here - doing so would
        # hand the stored token to whatever answers at the announced address.  If the PC
        # really moved, the user is told (a repair) and confirms the new address there.
        for entry in self._async_current_entries(include_ignore=True):
            if entry.unique_id == pc_id:
                if (entry.data.get(CONF_HOST), entry.data.get(CONF_PORT)) != (host, port):
                    _LOGGER.debug("%s announced itself at %s:%s but is configured at %s:%s; left unchanged",
                                  name, host, port, entry.data.get(CONF_HOST), entry.data.get(CONF_PORT))
                    self._note_moved(entry, host, port)
                return self.async_abort(reason="already_configured")
        self._discovered = {CONF_HOST: host, CONF_PORT: port, "name": name, "id": pc_id}
        if tls:
            self._discovered[CONF_TLS] = True
            if fingerprint:
                self._discovered[CONF_FINGERPRINT] = fingerprint
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_zeroconf_confirm()

    @callback
    def _note_moved(self, entry: config_entries.ConfigEntry, host: str, port: int) -> None:
        """A configured PC announced itself at another address.  Worth telling the user only
        while the PC cannot be reached where it is configured (a PC with several adapters
        announces more addresses than the one in use); the issue goes away when it connects.

        Only the announced host and port are kept.  HTTPS and the certificate's fingerprint
        always stay the entry's own: an announcement that could swap the pinned certificate
        would let whoever sent it receive the token."""
        if entry.source == config_entries.SOURCE_IGNORE or entry.disabled_by:
            return
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator is not None and coordinator.online:
            return
        ir.async_create_issue(
            self.hass, DOMAIN, issue_id(ISSUE_MOVED, entry.entry_id), is_fixable=True,
            severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_MOVED,
            translation_placeholders={"name": entry.title, "old": f"{entry.data.get(CONF_HOST)}:{entry.data.get(CONF_PORT)}",
                                      "new": f"{host}:{port}"},
            data={"entry_id": entry.entry_id, "host": host, "port": port})

    async def async_step_zeroconf_confirm(self, user_input: Optional[dict[str, Any]] = None):
        errors: dict[str, str] = {}
        address = _shown(self._discovered)
        host, port, name = address[CONF_HOST], address[CONF_PORT], self._discovered["name"]
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            health, error = await try_connection(self.hass, address, token)
            if health is None:
                errors["base"] = error or "unknown"
            else:
                device = str(health["device"])
                if device != self._discovered.get("id"):
                    # What answered is not the PC that was announced: do not create an entry for it.
                    _LOGGER.warning("%s:%s was announced as %r but identifies itself as %r; not added",
                                    host, port, self._discovered.get("id"), device)
                    return self.async_abort(reason="wrong_device")
                await self.async_set_unique_id(device, raise_on_progress=False)  # the id the PC itself reports
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=str(health.get("name") or name), data={**address, CONF_TOKEN: token})
        # The announcement is unauthenticated: what it says about HTTPS is shown, so the user
        # can compare the fingerprint with the one the PC printed before the token is sent.
        return self.async_show_form(step_id="zeroconf_confirm", data_schema=TOKEN_SCHEMA, errors=errors,
                                    description_placeholders={
                                        "name": name, "host": f"{host}:{port}",
                                        "security": describe_security(address.get(CONF_TLS), address.get(CONF_FINGERPRINT))})

    # -- token changed --------------------------------------------------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: Optional[dict[str, Any]] = None):
        entry = self._current_entry()
        if entry is None:
            return self.async_abort(reason="unknown")
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            address = {CONF_PORT: DEFAULT_PORT, **_shown(dict(entry.data))}
            health, error = await try_connection(self.hass, address, token)
            if health is None:
                errors["base"] = error or "unknown"
            elif entry.unique_id and str(health["device"]) != entry.unique_id:
                errors["base"] = "wrong_device"
            else:
                return self.async_update_reload_and_abort(entry, data={**entry.data, CONF_TOKEN: token},
                                                          reason="reauth_successful")
        return self.async_show_form(step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors,
                                    description_placeholders={"name": entry.title})

    # -- address changed ------------------------------------------------------------------
    async def async_step_reconfigure(self, user_input: Optional[dict[str, Any]] = None):
        """Offered by Home Assistant 2024.4 and later; before that the same is done by adding
        the PC again (see ``async_step_user``) or through the repair issue (repairs.py)."""
        entry = self._current_entry()
        if entry is None:
            return self.async_abort(reason="unknown")
        errors: dict[str, str] = {}
        if user_input is not None:
            data, error = await async_validate_new_address(self.hass, entry, user_input)
            if data is None:
                errors["base"] = error or "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data=data, reason="reconfigure_successful")
        # Address and port are pre-filled; the token never is (it would travel to the browser).
        shown = _shown(dict(user_input or entry.data))
        return self.async_show_form(step_id="reconfigure",
                                    data_schema=self.add_suggested_values_to_schema(RECONFIGURE_SCHEMA, shown),
                                    errors=errors, description_placeholders=address_placeholders(entry))

    def _current_entry(self) -> Optional[config_entries.ConfigEntry]:
        entry_id = self.context.get("entry_id")
        return self.hass.config_entries.async_get_entry(entry_id) if entry_id else None


def keeps_token(entry: config_entries.ConfigEntry, address: dict[str, Any]) -> bool:
    """May the stored token be sent to ``address`` without the user typing it again?

    Only to the address the entry already uses, or to a PC that has to present the very
    certificate the entry has pinned.  Anywhere else the token would go out (``/api/health``
    needs it) before the PC could prove it is the right one - and the address may come from
    an unauthenticated announcement (the "PC moved" repair)."""
    pinned = entry.data.get(CONF_FINGERPRINT)
    if pinned and address.get(CONF_FINGERPRINT) == pinned:
        return True
    same = (str(address.get(CONF_HOST)).lower(), int(address.get(CONF_PORT) or DEFAULT_PORT)) == \
        (str(entry.data.get(CONF_HOST)).lower(), int(entry.data.get(CONF_PORT) or DEFAULT_PORT))
    return same


def address_placeholders(entry: config_entries.ConfigEntry) -> dict[str, str]:
    """Description placeholders of the address forms (Reconfigure, the repairs)."""
    return {"name": entry.title,
            "security": describe_security(entry.data.get(CONF_TLS), entry.data.get(CONF_FINGERPRINT))}


async def async_validate_new_address(hass: HomeAssistant, entry: config_entries.ConfigEntry,
                                     user_input: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Check a new address (and optionally token) for an existing entry.

    ``(entry data, None)`` when the PC the entry was made for answers there with a token
    it accepts, else ``(None, error key)``.  Shared by "Reconfigure" and the repair flow.
    The token may be left empty only where ``keeps_token`` allows it (``token_required``)."""
    try:
        address = read_address(user_input)
    except ValueError as exc:
        return None, str(exc)
    token = user_input.get(CONF_TOKEN)
    if not token:  # empty = keep the stored token, but not for just any address
        if not keeps_token(entry, address):
            return None, "token_required"
        token = entry.data.get(CONF_TOKEN, "")
    health, error = await try_connection(hass, address, token)
    if health is None:
        return None, error or "unknown"
    if entry.unique_id and str(health["device"]) != entry.unique_id:
        return None, "wrong_device"
    return {**address, CONF_TOKEN: token}, None
