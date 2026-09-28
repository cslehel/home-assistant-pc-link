"""pclink.* actions."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pclink.const import DOMAIN


async def setup_entry(hass: HomeAssistant, entry_data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_actions_build_the_expected_commands(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "testpc")})
    call = hass.services.async_call
    # with a single PC device_id may be omitted
    await call(DOMAIN, "add_minutes", {"user": "kevin", "minutes": 20}, blocking=True)
    await call(DOMAIN, "add_minutes", {"device_id": device.id, "user": "kevin", "minutes": 5, "pin": "1234"}, blocking=True)
    await call(DOMAIN, "set_limit", {"user": "kevin", "scope": "weekly"}, blocking=True)
    await call(DOMAIN, "set_limit", {"user": "kevin", "scope": "today", "minutes": 120}, blocking=True)
    await call(DOMAIN, "focus", {"user": "kevin", "minutes": 45, "allow": ["docs.google.com"]}, blocking=True)
    await call(DOMAIN, "volume", {"user": "kevin", "level": 30, "muted": False}, blocking=True)
    await call(DOMAIN, "notify", {"message": "Dinner", "title": "Hey"}, blocking=True)
    await call(DOMAIN, "power", {"action": "shutdown", "delay": 60, "message": "Night"}, blocking=True)
    await call(DOMAIN, "wake_on_lan", {"mac": "AA:BB:CC:DD:EE:FF"}, blocking=True)
    await call(DOMAIN, "rule_enabled", {"user": "kevin", "rule": "YouTube", "enabled": False}, blocking=True)
    await call(DOMAIN, "pin", {"pin": "1234"}, blocking=True)
    await call(DOMAIN, "launch", {"name": "Minecraft", "user": "kevin"}, blocking=True)
    assert client.commands == [
        {"action": "add_minutes", "user": "kevin", "minutes": 20.0},
        {"action": "add_minutes", "user": "kevin", "minutes": 5.0, "pin": "1234"},
        {"action": "set_limit", "user": "kevin", "scope": "weekly", "minutes": None},
        {"action": "set_limit", "user": "kevin", "scope": "today", "minutes": 120.0},
        {"action": "focus", "user": "kevin", "minutes": 45, "allow": ["docs.google.com"]},
        {"action": "volume", "level": 30, "user": "kevin"},
        {"action": "mute", "muted": False, "user": "kevin"},
        {"action": "notify", "message": "Dinner", "title": "Hey"},
        {"action": "shutdown", "delay": 60, "message": "Night"},
        {"action": "wol", "mac": "AA:BB:CC:DD:EE:FF"},
        {"action": "rule_enabled", "user": "kevin", "rule": "YouTube", "enabled": False},
        {"action": "pin", "pin": "1234"},
        {"action": "launch", "name": "Minecraft", "user": "kevin"},
    ]


async def test_raw_command_returns_the_response_and_refusals_raise(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    response = await hass.services.async_call(DOMAIN, "command", {"data": {"action": "reload"}}, blocking=True, return_response=True)
    assert response == {"results": {"testpc": {"ok": True, "echo": "reload"}}}
    client.refuse = "parent pin required"
    with pytest.raises(HomeAssistantError, match="parent pin required"):
        await hass.services.async_call(DOMAIN, "unblock", {"user": "kevin"}, blocking=True)


async def test_validation_errors(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "add_minutes", {"device_id": "not-a-device", "user": "kevin", "minutes": 5}, blocking=True)
    with pytest.raises(Exception):  # schema: volume needs level or muted
        await hass.services.async_call(DOMAIN, "volume", {"user": "kevin"}, blocking=True)
