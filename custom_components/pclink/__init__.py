"""PC Link: screen-time limits, presence, telemetry and control for Windows PCs.

Talks to the PC Link service on each PC over its local API (REST + WebSocket).
Entities are created from the catalog the PC
publishes, so new users, rules, apps or sensors appear without an update here.
"""
from __future__ import annotations

from typing import Optional

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .api import PclinkClient
from .const import (
    CONF_FINGERPRINT,
    CONF_TLS,
    CONF_TOKEN,
    DEFAULT_PORT,
    DOMAIN,
    ISSUE_MOVED,
    ISSUE_WRONG_PC,
    PLATFORMS,
    ROLLUP_DEVICE_ID,
    issue_id,
)
from .coordinator import PclinkCoordinator
from .rollup import MIN_PCS, configured_pcs, get_hub, remove_rollup_entities
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the ``pclink.*`` actions once, for as long as the integration is installed.

    They must exist even while no PC is reachable (a PC that is switched off at
    Home Assistant start leaves its entry in "retrying setup"): an automation
    then gets "the PC is not available" instead of "action not found"."""
    hass.data.setdefault(DOMAIN, {})
    async_setup_services(hass)
    return True


def client_for(hass: HomeAssistant, data: dict) -> PclinkClient:
    """The API client for an entry's data (address, token and - for HTTPS - the certificate's fingerprint)."""
    return PclinkClient(async_get_clientsession(hass), data[CONF_HOST], data.get(CONF_PORT, DEFAULT_PORT),
                        data.get(CONF_TOKEN), bool(data.get(CONF_TLS)), data.get(CONF_FINGERPRINT) or None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = PclinkCoordinator(hass, entry, client_for(hass, dict(entry.data)))
    await coordinator.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    # The "all PCs" roll-up (rollup.py): its entities are created by the platforms of one
    # entry - this one, when two or more PCs are configured and no entry holds them yet.
    hub = get_hub(hass)
    hub.attach(coordinator)
    if hub.owner_entry_id is None:
        if configured_pcs(hass) >= MIN_PCS:
            hub.owner_entry_id = entry.entry_id
        else:
            _drop_rollup(hass)  # left over from when there were more PCs
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # The PC's connection goes first: a catalog or state arriving while the platforms are torn
    # down would otherwise hand new entities to platforms that are gone.  The roll-up is
    # released before this PC leaves it, so that its last update is not sent to them either.
    coordinator: PclinkCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_shutdown()
    hub = get_hub(hass)
    released = hub.release(entry.entry_id)
    hub.detach(entry.entry_id)
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if released and entry.disabled_by:
            _hand_over_rollup(hass)  # switched off for good; a mere reload takes the roll-up back itself
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A PC was deleted: if it held the roll-up entities, another PC's entry takes them over;
    with fewer than two PCs left there is no roll-up.  What was stored for the PC goes too."""
    for kind in (ISSUE_WRONG_PC, ISSUE_MOVED):
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry.entry_id))
    try:
        await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    except Exception as exc:  # noqa: BLE001 - a leftover file is no reason to fail the removal
        _LOGGER.debug("could not remove the stored state of %s: %s", entry.title, exc)
    if configured_pcs(hass, leaving=entry.entry_id) < MIN_PCS:
        _drop_rollup(hass)
    else:
        _hand_over_rollup(hass, leaving=entry.entry_id)


@callback
def _drop_rollup(hass: HomeAssistant) -> None:
    """Fewer than two PCs: remove the roll-up's entities and its device, loaded or left in the registry."""
    hub = get_hub(hass)
    if hub.owner_entry_id is not None:
        hub.release(hub.owner_entry_id)
    if remove_rollup_entities(hass):
        _LOGGER.info("Removed the roll-up of all PCs: fewer than %d PCs are configured", MIN_PCS)


def _hand_over_rollup(hass: HomeAssistant, leaving: Optional[str] = None) -> None:
    hub = get_hub(hass)
    loaded = [entry_id for entry_id in hass.data.get(DOMAIN, {}) if entry_id != leaving]
    if hub.owner_entry_id is None and loaded and configured_pcs(hass, leaving) >= MIN_PCS:
        hass.config_entries.async_schedule_reload(loaded[0])  # its platforms create the entities on setup


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry) -> bool:
    """Allow deleting a stale device (e.g. left behind after the PC's ``device.id`` changed).

    The device the entry currently represents cannot be deleted; remove the entry instead.
    Neither can the roll-up device while this entry holds its entities."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    current = {(DOMAIN, coordinator.pc_id)} if coordinator is not None else {(DOMAIN, str(entry.unique_id))}
    if get_hub(hass).owner_entry_id == entry.entry_id:
        current.add((DOMAIN, ROLLUP_DEVICE_ID))
    return not (device.identifiers & current)
