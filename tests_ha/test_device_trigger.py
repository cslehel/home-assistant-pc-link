"""Device triggers ("kevin ran out of time")."""
from __future__ import annotations

from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_get_device_automations, async_mock_service

from custom_components.pclink.const import DOMAIN, EVENT_TYPE


async def test_triggers_listed_and_fire(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = next((d for d in dr.async_entries_for_config_entry(dr.async_get(hass), hass.config_entries.async_entries(DOMAIN)[0].entry_id) if (DOMAIN, "testpc") in d.identifiers), None)
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, device.id)
    kinds = {(t["type"], t.get("subtype")) for t in triggers}
    assert ("started", None) in kinds and ("time_up", "kevin") in kinds and ("logged_in", "kevin") in kinds

    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(hass, automation.DOMAIN, {automation.DOMAIN: [{
        "trigger": {"platform": "device", "domain": DOMAIN, "device_id": device.id, "type": "time_up", "subtype": "kevin"},
        "action": {"service": "test.automation", "data_template": {"who": "{{ trigger.event.data.user }}"}},
    }, {
        "trigger": {"platform": "device", "domain": DOMAIN, "device_id": device.id, "type": "started"},
        "action": {"service": "test.automation", "data": {"who": "pc"}},
    }]})
    client = fake_client.instances[-1]
    ws = client.ws
    ws.push({"type": "event", "ts": 1.0, "user": "kevin", "event": "time_up"})
    ws.push({"type": "event", "ts": 1.0, "user": "dad", "event": "time_up"})     # other user: no match
    ws.push({"type": "event", "ts": 1.0, "user": "kevin", "event": "warning"})   # other type: no match
    ws.push({"type": "event", "ts": 1.0, "event": "started"})
    await hass.async_block_till_done()
    await hass.async_block_till_done()  # the automations' actions run one step after the events
    assert [c.data["who"] for c in calls] == ["kevin", "pc"]
