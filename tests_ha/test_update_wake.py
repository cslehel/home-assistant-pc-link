"""The PC Link update entity, the Wake-on-LAN button and starting while the PC is switched off."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pclink import button as button_module
from custom_components.pclink.api import PclinkConnectionError
from custom_components.pclink.const import DOMAIN

from conftest import make_catalog, make_state

UPDATE = {"unique_id": "pclink_testpc_pclink_update", "key": "pclink_update", "platform": "update", "name": "PC Link update",
          "value": "pclink_installed_version", "latest": "pclink_latest_version", "in_progress": "pclink_update_in_progress",
          "release_url": "pclink_update_release_url", "summary": "pclink_update_summary", "title": "PC Link",
          "device_class": "firmware", "category": "config", "command": "pclink_update_install", "source": "pc",
          "attributes": "pclink_update_info"}


async def setup_entry(hass: HomeAssistant, entry_data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def push_update(client, latest="0.4.0", command=True, info=None) -> None:
    catalog = make_catalog()
    catalog["entities"].append(dict(UPDATE) if command else {k: v for k, v in UPDATE.items() if k != "command"})
    pc, users = make_state()
    pc.update(pclink_installed_version="0.3.0", pclink_latest_version=latest, pclink_update_in_progress=False,
              pclink_update_release_url="https://github.com/o/r/releases/tag/v" + latest, pclink_update_summary="Fixes",
              mac="AA:BB:CC:DD:EE:FF", ip="192.168.1.50",
              pclink_update_info=info or {"status": "idle", "error": None, "signature": None})
    client.ws.push({"type": "entities", **catalog})
    client.ws.push({"type": "state", "pc": pc, "users": users})


async def test_update_entity_shows_versions_and_installs(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    push_update(client)
    await hass.async_block_till_done()
    state = hass.states.get("update.testpc_pc_link_update")
    assert state is not None and state.state == "on"
    assert state.attributes["installed_version"] == "0.3.0" and state.attributes["latest_version"] == "0.4.0"
    assert state.attributes["release_url"].endswith("/v0.4.0") and state.attributes["release_summary"] == "Fixes"
    await hass.services.async_call("update", "install", {"entity_id": "update.testpc_pc_link_update"}, blocking=True)
    assert client.commands[-1] == {"action": "set", "entity": "pclink_update_install", "value": "PRESS"}

    # a refused update: the reason is an attribute of the entity, next to Home Assistant's own
    refusal = "update to 0.4.0 failed: the update must be signed by Acme Ltd, who signed the installed version, but pclink.exe is not signed"
    push_update(client, info={"status": "error", "error": refusal, "signature": refusal.split("failed: ", 1)[1]})
    await hass.async_block_till_done()
    state = hass.states.get("update.testpc_pc_link_update")
    assert state.state == "on" and state.attributes["status"] == "error" and state.attributes["error"] == refusal
    assert state.attributes["signature"].endswith("pclink.exe is not signed") and state.attributes["latest_version"] == "0.4.0"

    push_update(client, latest="0.3.0")
    await hass.async_block_till_done()
    state = hass.states.get("update.testpc_pc_link_update")
    assert state.state == "off" and state.attributes["error"] is None


async def test_update_entity_without_install_support(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    push_update(client, command=False)
    await hass.async_block_till_done()
    assert hass.states.get("update.testpc_pc_link_update").attributes["supported_features"] == 0
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("update", "install", {"entity_id": "update.testpc_pc_link_update"}, blocking=True)


def test_magic_packet_and_targets() -> None:
    packet = button_module.magic_packet("aa-bb-cc-dd-ee-ff")
    assert packet == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16 and len(packet) == 102
    with pytest.raises(ValueError):
        button_module.magic_packet("not a mac")
    assert button_module.wake_targets("192.168.1.50") == ["255.255.255.255", "192.168.1.255", "192.168.1.50"]
    assert button_module.wake_targets(None) == ["255.255.255.255"]


async def test_wake_button_and_start_while_the_pc_is_off(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    wake = "button.testpc_wake_up"
    assert hass.states.get(wake).state == "unavailable"       # no MAC address seen yet
    push_update(client)
    await hass.async_block_till_done()
    assert hass.states.get(wake).state != "unavailable"
    with patch.object(button_module, "send_magic_packet", return_value=6) as send:
        await hass.services.async_call("button", "press", {"entity_id": wake}, blocking=True)
    send.assert_called_once_with("AA:BB:CC:DD:EE:FF", "192.168.1.50", None)

    # Home Assistant restarts while the PC is switched off: the entry still loads from the cache.
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator._cache_store.async_save(coordinator._cache_data())
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    async def unreachable(self):
        raise PclinkConnectionError("PC is off")

    with patch.object(fake_client, "health", unreachable), patch.object(fake_client, "ws_connect", unreachable):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get("sensor.testpc_cpu_usage").state == "unavailable"
        assert hass.states.get(wake).state != "unavailable"
        with patch.object(button_module, "send_magic_packet", return_value=6) as send:
            await hass.services.async_call("button", "press", {"entity_id": wake}, blocking=True)
        send.assert_called_once_with("AA:BB:CC:DD:EE:FF", "192.168.1.50", None)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_unknown_pc_that_is_off_is_retried(hass: HomeAssistant, fake_client, entry_data) -> None:
    async def unreachable(self):
        raise PclinkConnectionError("PC is off")

    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    with patch.object(fake_client, "health", unreachable):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_usage_report_action_returns_the_report(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    response = await hass.services.async_call(DOMAIN, "usage_report", {"user": "kevin", "hours": 10},
                                              blocking=True, return_response=True)
    assert client.commands[-1] == {"action": "usage_report", "user": "kevin", "hours": 10.0}
    assert response["results"]["testpc"]["ok"] is True


async def test_block_and_unblock_actions(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    await hass.services.async_call(DOMAIN, "block_item", {"user": "kevin", "item": "youtube.com"}, blocking=True)
    assert client.commands[-1] == {"action": "block_item", "user": "kevin", "item": "youtube.com"}
    await hass.services.async_call(DOMAIN, "unblock_item", {"user": "kevin", "item": "youtube.com", "pin": "1234"}, blocking=True)
    assert client.commands[-1] == {"action": "unblock_item", "user": "kevin", "item": "youtube.com", "pin": "1234"}
