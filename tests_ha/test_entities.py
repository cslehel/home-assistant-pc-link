"""Setup, entity states, commands, push updates and events."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.pclink.const import DOMAIN, EVENT_TYPE


async def setup_entry(hass: HomeAssistant, entry_data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_are_created_from_the_catalog(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    # device
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "testpc")})
    assert device is not None and device.name == "TestPC" and device.model == "Windows PC" and device.sw_version == "0.3.0"
    assert device.configuration_url == "http://192.168.1.50:47392"
    # PC entities
    cpu = hass.states.get("sensor.testpc_cpu_usage")
    assert cpu is not None and cpu.state == "12.5" and cpu.attributes["unit_of_measurement"] == "%"
    assert cpu.attributes["state_class"] == "measurement" and cpu.attributes["icon"] == "mdi:cpu-64-bit"
    boot = hass.states.get("sensor.testpc_last_boot")
    assert boot.state == "2026-09-28T06:00:00+00:00" and boot.attributes["device_class"] == "timestamp"
    sessions = hass.states.get("sensor.testpc_active_sessions")
    assert sessions.state == "1" and sessions.attributes["sessions"] == [{"id": 2, "user": "kevin", "state": "active"}]
    assert hass.states.get("binary_sensor.testpc_someone_is_using_the_pc").state == "on"
    assert hass.states.get("switch.testpc_time_limits").state == "on"
    assert hass.states.get("button.testpc_reboot").attributes["device_class"] == "restart"
    assert hass.states.get("text.testpc_message_to_everyone").state == ""
    assert hass.states.get("text.testpc_parent_pin").attributes["mode"] == "password"
    # user entities
    status = hass.states.get("sensor.testpc_kevin_status")
    assert status.state == "active" and status.attributes["device_class"] == "enum" and "idle" in status.attributes["options"]
    remaining = hass.states.get("sensor.testpc_kevin_time_remaining")
    assert remaining.state == "42.4" and remaining.attributes["limit_minutes"] == 90
    assert hass.states.get("sensor.testpc_kevin_youtube").state == "ok"
    assert hass.states.get("sensor.testpc_kevin_youtube").attributes["limit_minutes"] == 30
    assert hass.states.get("binary_sensor.testpc_kevin_logged_in").state == "on"
    limit = hass.states.get("number.testpc_kevin_limit_today")
    assert limit.state == "90" and limit.attributes["max"] == 1440 and limit.attributes["step"] == 5
    assert hass.states.get("switch.testpc_kevin_access_paused").state == "off"
    assert hass.states.get("camera.testpc_kevin_screenshot").state == "unavailable"  # no screenshot yet
    # registry: categories and unique ids
    registry = er.async_get(hass)
    assert registry.async_get("switch.testpc_time_limits").entity_category == "config"
    assert registry.async_get("sensor.testpc_last_boot").entity_category == "diagnostic"
    assert registry.async_get("sensor.testpc_cpu_usage").unique_id == "pclink_testpc_cpu_percent"
    assert registry.async_get("sensor.testpc_cpu_usage").device_id == device.id
    # the websocket was opened and asked for state + catalog
    assert client.ws_connects == 1 and [m["type"] for m in client.ws.sent] == ["status", "entities"]
    # unload
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_cpu_usage").state == "unavailable"
    assert not hass.services.has_service(DOMAIN, "add_minutes")


async def test_commands_are_sent_as_set_actions(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.testpc_kevin_access_paused"}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.testpc_time_limits"}, blocking=True)
    await hass.services.async_call("number", "set_value", {"entity_id": "number.testpc_kevin_limit_today", "value": 120}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": "button.testpc_kevin_15_min"}, blocking=True)
    await hass.services.async_call("text", "set_value", {"entity_id": "text.testpc_kevin_message", "value": "Dinner!"}, blocking=True)
    await hass.services.async_call("text", "set_value", {"entity_id": "text.testpc_parent_pin", "value": "1234"}, blocking=True)
    assert client.commands == [
        {"action": "set", "entity": "user_kevin_blocked", "value": "ON"},
        {"action": "set", "entity": "time_limits_enabled", "value": "OFF"},
        {"action": "set", "entity": "user_kevin_limit_today", "value": 120},
        {"action": "set", "entity": "user_kevin_add_15", "value": "PRESS"},
        {"action": "set", "entity": "user_kevin_message", "value": "Dinner!"},
        {"action": "set", "entity": "parent_pin", "value": "1234"},
    ]
    assert hass.states.get("text.testpc_kevin_message").state == "Dinner!"
    assert hass.states.get("text.testpc_parent_pin").state == ""  # never echoed back
    # a refusal (parent PIN) becomes an error the UI shows
    client.refuse = "parent pin required"
    with pytest.raises(HomeAssistantError, match="parent pin required"):
        await hass.services.async_call("button", "press", {"entity_id": "button.testpc_kevin_15_min"}, blocking=True)


async def test_pushed_state_events_and_new_entities(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    ws = client.ws
    # state push updates entities
    pc, users = dict(client.pc_state), {"kevin": dict(client.user_state["kevin"])}
    pc["cpu_percent"] = 55
    users["kevin"]["remaining_minutes"] = 10
    users["kevin"]["blocked"] = True
    users["kevin"]["screenshot"] = {"ts": "2026-09-28T12:00:00", "format": "jpeg", "width": 640, "height": 360, "bytes": 8}
    ws.push({"type": "hello", "writable": True, "status": client.health_doc})
    ws.push({"type": "state", "pc": pc, "users": users})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_cpu_usage").state == "55"
    assert hass.states.get("sensor.testpc_kevin_time_remaining").state == "10"
    assert hass.states.get("switch.testpc_kevin_access_paused").state == "on"
    camera = hass.states.get("camera.testpc_kevin_screenshot")
    assert camera.state != "unavailable" and camera.attributes["width"] == 640
    from homeassistant.components.camera import async_get_image
    image = await async_get_image(hass, "camera.testpc_kevin_screenshot")
    assert image.content == b"JPEGDATA" and image.content_type == "image/jpeg"
    # events reach the bus with the device id
    events = async_capture_events(hass, EVENT_TYPE)
    ws.push({"type": "event", "ts": 1.0, "user": "kevin", "event": "time_up"})
    await hass.async_block_till_done()
    assert len(events) == 1
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "testpc")})
    assert events[0].data["device_id"] == device.id and events[0].data["event"] == "time_up" and events[0].data["user"] == "kevin"
    assert events[0].data["pc_id"] == "testpc"
    # a new catalog (e.g. after a config reload on the PC) adds entities without a restart
    catalog = client.catalog
    catalog["entities"].append({"unique_id": "pclink_testpc_user_sarah_status", "key": "user_sarah_status", "platform": "sensor",
                               "name": "Sarah status", "value": "status", "source": "user", "user": "sarah", "user_name": "Sarah"})
    catalog["users"]["sarah"] = {"name": "Sarah", "managed": True}
    users["sarah"] = {"user": "sarah", "status": "idle"}
    ws.push({"type": "entities", **catalog})
    ws.push({"type": "state", "pc": pc, "users": users})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_sarah_status").state == "idle"
    assert hass.states.get("sensor.testpc_cpu_usage").state == "55"  # existing ones untouched


async def test_lost_connection_marks_entities_unavailable(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    ws = client.ws
    ws.push({"type": "hello", "writable": True, "status": client.health_doc})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_cpu_usage").state == "12.5"
    from custom_components.pclink.api import PclinkConnectionError
    client.fail = PclinkConnectionError("gone")  # REST also fails now
    await ws.close()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_cpu_usage").state == "unavailable"
    assert hass.states.get("button.testpc_reboot").state == "unavailable"
