"""pclink.* actions."""
from __future__ import annotations

import pytest
import voluptuous as vol
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
    device = next((d for d in dr.async_entries_for_config_entry(dr.async_get(hass), hass.config_entries.async_entries(DOMAIN)[0].entry_id) if (DOMAIN, "testpc") in d.identifiers), None)
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
    with pytest.raises(vol.Invalid):  # schema: volume needs level or muted
        await hass.services.async_call(DOMAIN, "volume", {"user": "kevin"}, blocking=True)


async def test_empty_user_is_rejected_not_broadcast(hass: HomeAssistant, fake_client, entry_data) -> None:
    """An empty ``user`` (a template that rendered to "") must not become "every session"."""
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    for user in ("", "   "):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "logoff", {"user": user}, blocking=True)
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "block", {"user": user}, blocking=True)
    assert client.commands == []
    await hass.services.async_call(DOMAIN, "logoff", {"user": " kevin "}, blocking=True)  # surrounding blanks are dropped
    await hass.services.async_call(DOMAIN, "logoff", {}, blocking=True)                   # omitted = everyone, on purpose
    assert client.commands == [{"action": "logoff", "user": "kevin"}, {"action": "logoff"}]


async def test_actions_survive_an_unreachable_pc(hass: HomeAssistant, fake_client, entry_data) -> None:
    """With the only PC unloaded the action still exists and says the PC is not available."""
    entry = await setup_entry(hass, entry_data)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "add_minutes")
    with pytest.raises(HomeAssistantError, match="not available"):
        await hass.services.async_call(DOMAIN, "add_minutes", {"user": "kevin", "minutes": 5}, blocking=True)


async def test_rule_editing_and_display_actions(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    call = hass.services.async_call
    response = await call(DOMAIN, "set_rule", {"user": "kevin", "rule": " Roblox ", "apps": "RobloxPlayerBeta.exe", "sites": ["roblox.com"],
                                               "daily_limit_minutes": {"weekdays": 30, "weekend": 60}, "weekly_limit_minutes": 300,
                                               "allowed_hours": "16:00-19:00", "rule_action": "warn"}, blocking=True, return_response=True)
    assert response == {"results": {"testpc": {"ok": True, "echo": "rule_set"}}}
    await call(DOMAIN, "set_rule", {"user": "kevin", "rule": "YouTube", "daily_limit_minutes": "unlimited", "blocked": True, "pin": "1234"}, blocking=True)
    await call(DOMAIN, "remove_rule", {"user": "kevin", "rule": "Roblox"}, blocking=True)
    await call(DOMAIN, "rule_limit", {"user": "kevin", "rule": "YouTube", "scope": "weekly", "minutes": 200}, blocking=True)
    await call(DOMAIN, "rule_limit", {"user": "kevin", "rule": "YouTube", "minutes": 20}, blocking=True)
    await call(DOMAIN, "display", {"user": "kevin", "enabled": False}, blocking=True)
    await call(DOMAIN, "display", {"enabled": True}, blocking=True)
    assert client.commands == [
        {"action": "rule_set", "user": "kevin", "rule": "Roblox", "apps": ["RobloxPlayerBeta.exe"], "sites": ["roblox.com"],
         "daily_limit_minutes": {"weekdays": 30, "weekend": 60}, "weekly_limit_minutes": 300.0, "allowed_hours": "16:00-19:00",
         "rule_action": "warn"},
        {"action": "rule_set", "user": "kevin", "rule": "YouTube", "daily_limit_minutes": "unlimited", "blocked": True, "pin": "1234"},
        {"action": "rule_remove", "user": "kevin", "rule": "Roblox"},
        {"action": "rule_limit", "user": "kevin", "rule": "YouTube", "minutes": 200.0, "scope": "weekly"},
        {"action": "rule_limit", "user": "kevin", "rule": "YouTube", "minutes": 20.0},
        {"action": "display", "user": "kevin", "on": False},
        {"action": "display", "on": True},
    ]
    with pytest.raises(vol.Invalid):
        await call(DOMAIN, "set_rule", {"user": "kevin", "rule": "X", "rule_action": "explode"}, blocking=True)
    with pytest.raises(vol.Invalid):
        await call(DOMAIN, "display", {"user": "kevin"}, blocking=True)
