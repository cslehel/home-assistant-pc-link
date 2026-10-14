"""Tests for the fixes of the 2026-10 review: identity check, diagnostics, catalog hash, event
replay, the "all PCs" target, update progress, roll-up statistics and clean-up, HTTPS, repairs."""
from __future__ import annotations

import importlib.util
import json
import os
import time
from ipaddress import ip_address
from typing import Any, Optional
from unittest.mock import patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events, async_get_device_automations

from custom_components.pclink import button as button_module
from custom_components.pclink.api import (
    PclinkAuthError,
    PclinkClient,
    PclinkConnectionError,
    normalize_fingerprint,
    split_address,
)
from custom_components.pclink.const import DOMAIN, EVENT_TYPE, ROLLUP_UNIQUE_PREFIX
from custom_components.pclink.diagnostics import async_get_config_entry_diagnostics
from custom_components.pclink.rollup import build_rollup, get_hub

from conftest import make_catalog, make_state
from test_rollup import add_pc, rollup_entities, several_pcs
from test_update_wake import UPDATE

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2024.11
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FP = "ab" * 32


async def setup_entry(hass: HomeAssistant, entry_data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def hello(client) -> None:
    client.ws.push({"type": "hello", "writable": True, "status": client.health_doc})


def issue(hass: HomeAssistant, kind: str, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")


# -- 1: a different PC at the configured address -------------------------------------------------
async def test_a_different_pc_is_ignored_and_reported(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)
    before = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    hello(client)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_cpu_usage").state == "12.5" and coordinator.online

    # the socket's hello now comes from another PC (same token, the address was handed to it)
    client.ws.push({"type": "hello", "writable": True, "status": {**client.health_doc, "device": "den", "name": "Den"}})
    client.ws.push({"type": "state", "pc": {"cpu_percent": 99}, "users": {}})
    client.ws.push({"type": "entities", "device": {}, "users": {}, "entities": [
        {"unique_id": "pclink_den_cpu_percent", "key": "cpu_percent", "platform": "sensor", "name": "CPU usage", "value": "cpu_percent"}]})
    await hass.async_block_till_done()
    assert coordinator.wrong_pc == {"device": "den", "name": "Den"} and not coordinator.online
    assert hass.states.get("sensor.testpc_cpu_usage").state == "unavailable"       # not 99: its state is not taken
    assert coordinator.pc_id == "testpc" and coordinator.catalog_keys >= {"cpu_percent", "user_kevin_status"}
    found = issue(hass, "wrong_pc", entry)
    assert found is not None and found.is_fixable
    assert found.translation_placeholders["found_name"] == "Den" and found.translation_placeholders["name"] == "TestPC"
    # nothing is sent to it, by an entity or by an action
    sent = len(client.commands)
    with pytest.raises(HomeAssistantError, match="different PC"):
        await coordinator.live["time_limits_enabled"].async_turn_on()
    with pytest.raises(HomeAssistantError, match="different PC"):
        await hass.services.async_call(DOMAIN, "lock", {}, blocking=True)
    assert len(client.commands) == sent
    # and nothing is removed, however long it lasts
    coordinator._reconcile_registry()
    assert {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)} == before
    # the right PC again: the issue goes away
    assert coordinator._check_identity(client.health_doc) and coordinator.wrong_pc is None
    assert issue(hass, "wrong_pc", entry) is None


async def test_a_different_pc_at_start_loads_from_the_cache(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    assert await hass.config_entries.async_unload(entry.entry_id)   # the unload stores what is known
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    before = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}

    async def other_pc(self):
        return {"ok": True, "version": "0.3.0", "device": "den", "name": "Den"}

    with patch.object(fake_client, "health", other_pc):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get("sensor.testpc_cpu_usage").state == "unavailable"
        assert issue(hass, "wrong_pc", entry) is not None
        assert {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)} == before
        assert hass.data[DOMAIN][entry.entry_id].pc_id == "testpc"
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


# -- 2: diagnostics by allow-list ----------------------------------------------------------------
async def test_diagnostics_keep_structure_and_drop_every_free_text(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, {**entry_data, "tls": True, "fingerprint": FP})
    client = fake_client.instances[-1]
    pc, users = make_state()
    pc.update(mac="AA:BB:CC:DD:EE:FF", ip="192.168.1.50", gpu_name="Secret GPU", hw_sensor_count=0,
              service_info={"version": "0.3.0", "managed_users": ["kevin-secret"], "config": "C:\\Users\\dad-secret\\config.yaml"},
              fleet={"peers": ["den-secret"]})
    users["kevin"].update(
        date="2026-10-13", used_minutes=42.5, top_apps={"secret-game.exe": 12}, top_sites={"secret-site.com": 3},
        usage_recent={"user": "kevin", "total_minutes": 50, "from": "2026-10-13T08:00", "apps": [{"name": "secret-app.exe", "minutes": 5}],
                      "sites": [{"name": "secret-site.com", "minutes": 4}]},
        blocked_details={"items": ["secret-blocked.com"], "sites": ["secret-blocked.com"], "apps": []},
        focus_details={"source": "manual", "allow_sites": ["secret-allowed.org"], "allow_apps": ["secret-allowed.exe"]},
        presence_details={"state": "active", "reason": "typing in secret-window", "idle_seconds": 3},
        a_field_added_later="secret-future-value")
    hello(client)
    client.ws.push({"type": "state", "pc": pc, "users": users})
    await hass.async_block_till_done()
    data = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(data)
    assert "secret" not in text.lower() and "kevin" not in text and "AA:BB" not in text and "192.168" not in text and FP not in text
    user = data["users"]["user_1"]
    assert user["used_minutes"] == 42.5 and user["status"] == "active" and user["date"] == "2026-10-13"
    assert user["presence_details"] == {"state": "active", "reason": "**REDACTED**", "idle_seconds": 3}
    assert user["top_apps"] == {"item_1": 12}                       # names used as keys are numbered
    assert user["usage_recent"]["apps"] == [{"name": "**REDACTED**", "minutes": 5}] and user["usage_recent"]["total_minutes"] == 50
    assert user["a_field_added_later"] == "**REDACTED**"            # unknown text is redacted, not passed through
    assert data["pc"]["cpu_percent"] == 12.5 and data["pc"]["service_info"]["version"] == "0.3.0"
    assert data["health"]["version"] == "0.3.0" and data["health"]["device"] == "**REDACTED**"


# -- 3 / C10: the catalog follows the PC's set of entities ---------------------------------------
async def test_catalog_is_fetched_when_the_state_names_another_hash(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._catalog_min_wait = 0
    fetches: list[int] = []
    original = client.entities

    async def counted():
        fetches.append(1)
        return await original()

    client.entities = counted
    pc, users = make_state()
    client.catalog["hash"] = "h2"
    client.catalog["entities"].append({"unique_id": "pclink_testpc_hw_cpu_temp", "key": "hw_cpu_temp", "platform": "sensor",
                                       "name": "CPU temperature", "value": "hw_cpu_temp", "unit": "°C", "source": "pc"})
    hello(client)
    client.ws.push({"type": "state", "pc": {**pc, "catalog_hash": "h2", "hw_cpu_temp": 51}, "users": users})
    await hass.async_block_till_done()
    await coordinator._catalog_task
    await hass.async_block_till_done()
    assert fetches == [1] and coordinator.catalog["hash"] == "h2"
    assert hass.states.get("sensor.testpc_cpu_temperature").state == "51"
    # the same hash again: nothing to fetch
    client.ws.push({"type": "state", "pc": {**pc, "catalog_hash": "h2", "hw_cpu_temp": 52}, "users": users})
    await hass.async_block_till_done()
    assert fetches == [1]
    # a PC whose two sides never agree is asked once per hash, not with every state
    for _ in range(3):
        client.ws.push({"type": "state", "pc": {**pc, "catalog_hash": "h3"}, "users": users})
        await hass.async_block_till_done()
        await coordinator._catalog_task
    assert fetches == [1, 1]


async def test_old_service_new_hardware_sensors_trigger_a_refresh(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._catalog_min_wait = 0
    pc, users = make_state()
    assert coordinator._state_catalog_signature()[1] is False           # nothing hardware-related in the state
    client.catalog["entities"].append({"unique_id": "pclink_testpc_hw_fan", "key": "hw_fan", "platform": "sensor", "name": "Fan",
                                       "value": "hw_fan", "unit": "RPM", "source": "pc"})
    hello(client)
    client.ws.push({"type": "state", "pc": {**pc, "hw_fan": 900, "hw_sensor_count": 1}, "users": users})   # no catalog_hash
    await hass.async_block_till_done()
    await coordinator._catalog_task
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_fan").state == "900"
    assert coordinator._state_catalog_signature()[1] is False


async def test_a_catalog_request_during_a_refresh_is_not_lost(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._catalog_min_wait = 0
    fetches: list[int] = []
    original = client.entities

    async def slow():
        fetches.append(1)
        if len(fetches) == 1:
            coordinator._schedule_catalog_refresh()   # e.g. "config_reloaded" while the first answer is on its way
        return await original()

    client.entities = slow
    coordinator._schedule_catalog_refresh()
    await coordinator._catalog_task
    assert len(fetches) == 2


# -- 7: an empty catalog removes nothing ---------------------------------------------------------
async def test_an_empty_catalog_never_starts_the_removal_countdown(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)
    before = len(er.async_entries_for_config_entry(registry, entry.entry_id))
    hello(client)
    client.ws.push({"type": "entities", "device": {}, "users": {}, "entities": []})
    await hass.async_block_till_done()
    assert coordinator._missing == {} and coordinator._unsub_recheck is None
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == before
    # a catalog that merely lacks one entry does start it
    catalog = make_catalog()
    catalog["entities"] = [e for e in catalog["entities"] if e["key"] != "reboot"]
    client.ws.push({"type": "entities", **catalog})
    await hass.async_block_till_done()
    assert set(coordinator._missing) == {"pclink_testpc_reboot"}


# -- 4 / C12: events recorded while disconnected are replayed ------------------------------------
async def test_missed_events_are_replayed_once(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    events = async_capture_events(hass, EVENT_TYPE)
    now = time.time()
    # the first connection ever: nothing counts as missed
    client.recorded_events = [{"ts": now - 500, "user": "kevin", "kind": "time_up", "detail": ""}]
    hello(client)
    await hass.async_block_till_done()
    await coordinator._replay_task
    assert events == [] and coordinator.last_event_ts is not None

    coordinator.last_event_ts = now - 100
    client.ws.push({"type": "event", "ts": now - 60, "user": "kevin", "event": "warning"})      # arrived live
    await hass.async_block_till_done()
    assert len(events) == 1 and "replayed" not in events[0].data
    client.recorded_events = [                                                                    # newest first, as the PC lists them
        {"type": "event", "ts": now - 10, "user": "kevin", "event": "tamper", "reason": "agent_killed"},   # the pushed shape
        {"ts": now - 20, "user": None, "kind": "started", "detail": ""},                                    # the event log's shape
        {"ts": now - 40, "user": "kevin", "kind": "time_up", "detail": "limit"},
        {"ts": now - 50, "user": "kevin", "kind": "rule_set", "detail": "log entry, not an event"},
        {"ts": now - 59.7, "user": "kevin", "kind": "warning", "detail": ""},                   # the live one, as recorded
        {"ts": now - 300, "user": "kevin", "kind": "logged_in", "detail": ""},                  # before the last one seen
        {"ts": now - 90000, "user": "kevin", "kind": "bedtime", "detail": ""},
    ]
    hello(client)                                                                                 # a reconnect
    await hass.async_block_till_done()
    await coordinator._replay_task
    await hass.async_block_till_done()
    replayed = [(e.data["event"], e.data.get("user"), e.data["replayed"]) for e in events[1:]]
    assert replayed == [("time_up", "kevin", True), ("started", None, True), ("tamper", "kevin", True)]   # oldest first
    assert events[1].data["device_id"] == coordinator.device_id and events[1].data["pc_id"] == "testpc"
    assert events[3].data["reason"] == "agent_killed" and "type" not in events[3].data
    assert coordinator.last_event_ts == now - 10
    # another reconnect: nothing twice
    hello(client)
    await hass.async_block_till_done()
    await coordinator._replay_task
    assert len(events) == 4
    # ... and not after a restart of Home Assistant either: the time stamp is stored
    stored = coordinator._cache_data()
    assert stored["last_event_ts"] == now - 10 and [now - 20, "started", ""] in stored["events_seen"]


# -- 5 / idea 6: the "all PCs" device as a target ------------------------------------------------
async def test_all_pcs_device_fans_out_or_is_refused(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        first = await add_pc(hass, "192.168.1.50")
        second = await add_pc(hass, "192.168.1.51")
        testpc = next(c for c in fake_client.instances if c.host == "192.168.1.50")
        den = next(c for c in fake_client.instances if c.host == "192.168.1.51")
        rollup = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), second.entry_id)
                      if (DOMAIN, "all_pcs") in d.identifiers)
        await hass.services.async_call(DOMAIN, "lock", {"device_id": rollup.id, "user": "kevin"}, blocking=True)
        assert testpc.commands[-1] == {"action": "lock", "user": "kevin"} == den.commands[-1]
        # the same through target: (an entity of the roll-up) and with an ordinary PC next to it
        await hass.services.async_call(DOMAIN, "notify", {"message": "Dinner"},
                                       target={"entity_id": "sensor.pc_link_all_pcs_pcs_online"}, blocking=True)
        assert testpc.commands[-1] == {"action": "notify", "message": "Dinner"} == den.commands[-1]
        sent = (len(testpc.commands), len(den.commands))
        with pytest.raises(ServiceValidationError, match="all PCs"):
            await hass.services.async_call(DOMAIN, "add_minutes", {"device_id": rollup.id, "user": "kevin", "minutes": 10}, blocking=True)
        with pytest.raises(ServiceValidationError, match="all PCs"):
            await hass.services.async_call(DOMAIN, "answer_time_request", {"device_id": rollup.id, "user": "kevin", "approve": True}, blocking=True)
        assert (len(testpc.commands), len(den.commands)) == sent
        # a PC's own entity or device as target reaches that PC only
        await hass.services.async_call(DOMAIN, "add_minutes", {"user": "kevin", "minutes": 10},
                                       target={"entity_id": "sensor.den_cpu_usage"}, blocking=True)
        assert den.commands[-1] == {"action": "add_minutes", "user": "kevin", "minutes": 10.0} and len(testpc.commands) == sent[0]
        with pytest.raises(ServiceValidationError, match="not a PC Link entity"):
            await hass.services.async_call(DOMAIN, "lock", {}, target={"entity_id": "sun.sun"}, blocking=True)
        # the roll-up device offers no device triggers (no PC's events are its own)
        from homeassistant.components.device_automation import DeviceAutomationType

        async def own_triggers(device_id: str) -> list[dict[str, Any]]:
            found = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, device_id)
            return [t for t in found if t["domain"] == DOMAIN]   # sensors bring triggers of their own

        assert await own_triggers(rollup.id) == []
        pc_device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), first.entry_id)
                         if (DOMAIN, "testpc") in d.identifiers)
        assert await own_triggers(pc_device.id)


async def test_answer_time_request_and_pin_fields(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    call = hass.services.async_call
    await call(DOMAIN, "answer_time_request", {"user": "kevin", "approve": True, "minutes": 20, "pin": "1234"}, blocking=True)
    await call(DOMAIN, "answer_time_request", {"user": "kevin", "approve": False}, blocking=True)
    await call(DOMAIN, "parent_present", {"present": True, "pin": "1234"}, blocking=True)
    assert client.commands == [
        {"action": "time_request_answer", "user": "kevin", "approve": True, "minutes": 20, "pin": "1234"},
        {"action": "time_request_answer", "user": "kevin", "approve": False},
        {"action": "parent_present", "present": True, "pin": "1234"},
    ]
    import yaml

    with open(os.path.join(ROOT, "custom_components", "pclink", "services.yaml"), encoding="utf-8") as fh:
        fields = yaml.safe_load(fh)
    assert set(fields["answer_time_request"]["fields"]) == {"device_id", "user", "approve", "minutes", "pin"}
    assert "pin" in fields["parent_present"]["fields"] and "pin" in fields["focus"]["fields"]


# -- 6: update progress --------------------------------------------------------------------------
def _push_update(client, **pc_extra) -> None:
    catalog = make_catalog()
    catalog["entities"].append({**UPDATE, "release_notes": "pclink_update_notes", "percentage": "pclink_update_percent"})
    pc, users = make_state()
    pc.update({"pclink_installed_version": "0.3.0", "pclink_latest_version": "0.4.0", "pclink_update_in_progress": False,
               "pclink_update_notes": "## 0.4.0\n- better", **pc_extra})
    client.ws.push({"type": "entities", **catalog})
    client.ws.push({"type": "state", "pc": pc, "users": users})


async def test_update_shows_the_pcs_progress_and_survives_a_dropped_connection(hass: HomeAssistant, fake_client, entry_data) -> None:
    from homeassistant.components.update import UpdateEntityFeature

    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    hello(client)
    _push_update(client)
    await hass.async_block_till_done()
    entity_id = "update.testpc_pc_link_update"
    state = hass.states.get(entity_id)
    features = UpdateEntityFeature(state.attributes["supported_features"])
    assert UpdateEntityFeature.INSTALL in features and UpdateEntityFeature.PROGRESS in features and UpdateEntityFeature.RELEASE_NOTES in features
    assert state.attributes["in_progress"] is False
    assert await coordinator.live["pclink_update"].async_release_notes() == "## 0.4.0\n- better"
    # the PC reports the download / installation: shown for as long as it lasts
    _push_update(client, pclink_update_in_progress=True, pclink_update_percent=40)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["in_progress"] is True
    assert coordinator.live["pclink_update"].update_percentage == 40
    _push_update(client)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["in_progress"] is False

    # the installer stops the service before the answer is out: in progress, not an error
    async def dropped(data):
        raise PclinkConnectionError("connection reset")

    with patch.object(client, "command", dropped):
        await hass.services.async_call("update", "install", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).attributes["in_progress"] is True
    # back with the new version: done
    _push_update(client, pclink_installed_version="0.4.0")
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "off" and state.attributes["in_progress"] is False
    # a PC that was not reachable in the first place still is an error
    client.fail = PclinkConnectionError("gone")
    await client.ws.close()
    await hass.async_block_till_done()
    assert not coordinator.online
    with pytest.raises(HomeAssistantError):
        await coordinator.live["pclink_update"].async_install(None, False)


# -- 8 / 10 / 23: the roll-up --------------------------------------------------------------------
def test_build_rollup_tells_pcs_with_the_same_name_apart() -> None:
    kevin = {"name": "Kevin", "date": "2026-09-28", "used_minutes": 30, "status": "active", "logged_in": True}
    result = build_rollup([{"id": "a", "name": "PC", "online": True, "users": {"kevin": kevin}},
                           {"id": "b", "name": "PC", "online": True, "users": {"kevin": {**kevin, "used_minutes": 12}}},
                           {"id": "c", "name": "Den", "online": True, "users": {}}], "2026-09-28")
    assert set(result["pcs"]) == {"PC (a)", "PC (b)", "Den"} and result["pcs_online"] == 3
    assert result["users"]["kevin"]["per_pc"] == {"PC (a)": 30, "PC (b)": 12}
    assert result["users"]["kevin"]["total_minutes_today"] == 42


async def test_rollup_total_keeps_the_minutes_of_a_pc_that_is_off_at_start(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        await add_pc(hass, "192.168.1.50")
        second = await add_pc(hass, "192.168.1.51")
        total = hass.states.get("sensor.pc_link_all_pcs_kevin_total_time_today")
        assert total.state == "42.5" and total.attributes["state_class"] == "total"
        assert dt_util.parse_datetime(total.attributes["last_reset"]) == dt_util.start_of_local_day()
        # Den is switched off and Home Assistant restarts (here: the entry is set up again)
        assert await hass.config_entries.async_unload(second.entry_id)
        await hass.async_block_till_done()

        async def unreachable(self):
            raise PclinkConnectionError("PC is off")

        den_class = type(next(c for c in fake_client.instances if c.host == "192.168.1.51"))
        original = den_class.health

        async def health(self):
            if self.host == "192.168.1.51":
                raise PclinkConnectionError("PC is off")
            return await original(self)

        with patch.object(den_class, "health", health), patch.object(den_class, "ws_connect", unreachable):
            assert await hass.config_entries.async_setup(second.entry_id)
            await hass.async_block_till_done()
            den = hass.data[DOMAIN][second.entry_id]
            assert not den.online and den.users["kevin"]["used_minutes"] == 12.5
            assert set(den.users["kevin"]) <= {"name", "date", "used_minutes", "gaming_minutes", "browsing_minutes", "history"}
            total = hass.states.get("sensor.pc_link_all_pcs_kevin_total_time_today")
            assert total.state == "42.5" and total.attributes["per_pc"] == {"TestPC": 30, "Den": 12.5}
            assert hass.states.get("sensor.pc_link_all_pcs_pcs_online").state == "1"
            assert await hass.config_entries.async_unload(second.entry_id)
            await hass.async_block_till_done()


async def test_rollup_is_removed_when_one_pc_is_left(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        first = await add_pc(hass, "192.168.1.50")
        second = await add_pc(hass, "192.168.1.51")
        assert set(rollup_entities(hass).values()) == {second.entry_id}
        # the PC that does NOT hold the roll-up is deleted: the holder alone needs none either
        assert await hass.config_entries.async_remove(first.entry_id)
        await hass.async_block_till_done()
        assert rollup_entities(hass) == {} and get_hub(hass).owner_entry_id is None
        assert hass.states.get("sensor.pc_link_all_pcs_pcs_online") is None
        assert not [d for d in dr.async_entries_for_config_entry(dr.async_get(hass), second.entry_id)
                    if (DOMAIN, "all_pcs") in d.identifiers]
        assert hass.states.get("sensor.den_cpu_usage").state == "12.5"


async def test_rollup_entities_of_a_user_no_pc_knows_are_removed_after_the_grace_period(hass: HomeAssistant, fake_client, entry_data) -> None:
    from custom_components.pclink import rollup

    with several_pcs(fake_client):
        await add_pc(hass, "192.168.1.50")
        await add_pc(hass, "192.168.1.51")
        testpc = next(c for c in fake_client.instances if c.host == "192.168.1.50")
        den = next(c for c in fake_client.instances if c.host == "192.168.1.51")
        for client in (testpc, den):
            hello(client)
        await hass.async_block_till_done()
        assert "user:sarah:status" in rollup_entities(hass)
        users = {k: v for k, v in den.user_state.items() if k != "sarah"}
        den.ws.push({"type": "state", "pc": den.pc_state, "users": users})
        await hass.async_block_till_done()
        assert "user:sarah:status" in rollup_entities(hass)          # not on first sight
        with patch.object(rollup, "CATALOG_REMOVE_GRACE_SECONDS", 0):
            den.ws.push({"type": "state", "pc": den.pc_state, "users": users})
            await hass.async_block_till_done()
        left = rollup_entities(hass)
        assert not any(uid.startswith("user:sarah:") for uid in left) and "user:kevin:status" in left


# -- 9: peer usage ---------------------------------------------------------------------------------
async def test_failed_peer_usage_backs_off_and_asks_for_a_new_token(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        await add_pc(hass, "192.168.1.50")
        second = await add_pc(hass, "192.168.1.51")
        den = next(c for c in fake_client.instances if c.host == "192.168.1.51")
        attempts: list[dict[str, Any]] = []

        async def rejected(data):
            attempts.append(data)
            raise PclinkAuthError("token rejected")

        hub = get_hub(hass)
        with patch.object(den, "send", rejected):
            hello(den)
            for _ in range(4):   # every state of every PC used to be another failing request
                den.ws.push({"type": "state", "pc": {**den.pc_state, "shared_budget": True}, "users": den.user_state})
                await hass.async_block_till_done()
        assert len(attempts) == 1
        failures, not_before = hub._peer_retry[second.entry_id]
        assert failures == 1 and not_before > time.monotonic() + 20
        assert any(flow["context"]["source"] == config_entries.SOURCE_REAUTH for flow in hass.config_entries.flow.async_progress())
        hub.stop()


# -- 11 / 12 / 14: state handling ------------------------------------------------------------------
async def test_a_poll_answer_does_not_replace_newer_pushed_state(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    original = client.status

    async def status_while_the_socket_comes_up():
        stale = await original()                       # cpu 12.5, on its way ...
        coordinator._handle_message({"type": "state", "pc": {**stale, "cpu_percent": 77}, "users": client.user_state})
        return stale

    with patch.object(client, "status", status_while_the_socket_comes_up):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.pc["cpu_percent"] == 77 and hass.states.get("sensor.testpc_cpu_usage").state == "77"


async def test_set_values_show_at_once_and_the_state_is_asked_for_over_the_socket(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    hello(client)
    await hass.async_block_till_done()
    asked = len(client.ws.sent)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.testpc_kevin_access_paused"}, blocking=True)
    assert hass.states.get("switch.testpc_kevin_access_paused").state == "on"       # before the PC said anything
    assert [m["type"] for m in client.ws.sent[asked:]] == ["status"]
    await hass.services.async_call("number", "set_value", {"entity_id": "number.testpc_kevin_limit_today", "value": 120}, blocking=True)
    assert hass.states.get("number.testpc_kevin_limit_today").state == "120.0"
    # the PC's word counts: here it did not take the pause
    client.ws.push({"type": "status", "pc": client.pc_state, "users": client.user_state})
    await hass.async_block_till_done()
    assert hass.states.get("switch.testpc_kevin_access_paused").state == "off"
    assert hass.states.get("number.testpc_kevin_limit_today").state == "90"


# -- C: select platform ------------------------------------------------------------------------------
async def test_select_entities(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    catalog = make_catalog()
    catalog["entities"].append({"unique_id": "pclink_testpc_user_kevin_mode", "key": "user_kevin_mode", "platform": "select",
                               "name": "kevin mode", "value": "mode", "options": ["school", "holiday", "weekend"],
                               "category": "config", "command": "user_kevin_mode", "source": "user", "user": "kevin"})
    users = json.loads(json.dumps(client.user_state))
    users["kevin"]["mode"] = "school"
    hello(client)
    client.ws.push({"type": "entities", **catalog})
    client.ws.push({"type": "state", "pc": client.pc_state, "users": users})
    await hass.async_block_till_done()
    state = hass.states.get("select.testpc_kevin_mode")
    assert state.state == "school" and state.attributes["options"] == ["school", "holiday", "weekend"]
    await hass.services.async_call("select", "select_option", {"entity_id": "select.testpc_kevin_mode", "option": "holiday"}, blocking=True)
    assert client.commands[-1] == {"action": "set", "entity": "user_kevin_mode", "value": "holiday"}
    assert hass.states.get("select.testpc_kevin_mode").state == "holiday"
    # the options follow the catalog
    client.ws.push({"type": "state", "pc": client.pc_state, "users": users})
    catalog["entities"][-1] = {**catalog["entities"][-1], "options": ["school", "holiday"]}
    client.ws.push({"type": "entities", **catalog})
    await hass.async_block_till_done()
    assert hass.states.get("select.testpc_kevin_mode").attributes["options"] == ["school", "holiday"]


# -- 15: storage ---------------------------------------------------------------------------------------
async def test_stored_state_is_removed_with_the_entry_and_a_damaged_one_is_ignored(hass: HomeAssistant, fake_client, entry_data,
                                                                                    hass_storage) -> None:
    entry = await setup_entry(hass, entry_data)
    key = f"{DOMAIN}.{entry.entry_id}"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert key in hass_storage and hass_storage[key]["data"]["catalog"]["entities"]
    # parts of the wrong type are dropped one by one instead of breaking the start
    hass_storage[key]["data"].update(health=["not", "a", "dict"], mac=17, users="nobody", last_event_ts="yesterday", events_seen=[[1], "x"])

    async def unreachable(self):
        raise PclinkConnectionError("PC is off")

    with patch.object(fake_client, "health", unreachable), patch.object(fake_client, "ws_connect", unreachable):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.health == {} and coordinator.wake_mac is None and coordinator.users == {} and coordinator.pc_id == "testpc"
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert key not in hass_storage


# -- 18 / C9 / C7: addresses, HTTPS, the auth probe ----------------------------------------------------
def test_split_address_and_fingerprint() -> None:
    assert split_address(" 192.168.1.5 ") == ("192.168.1.5", None, None)
    assert split_address("http://192.168.1.5:47392/") == ("192.168.1.5", 47392, False)
    assert split_address("https://pc.local/setup") == ("pc.local", None, True)
    assert split_address("pc.local:1234") == ("pc.local", 1234, None)
    assert split_address("[fe80::1]:47392") == ("fe80::1", 47392, None)
    assert split_address("fe80::1") == ("fe80::1", None, None)
    for bad in ("", "   ", "http://", "pc.local:port", "ftp://pc.local", "two words", "pc.local:70000"):
        with pytest.raises(ValueError):
            split_address(bad)
    assert normalize_fingerprint("AB:" * 31 + "AB") == FP and normalize_fingerprint("") is None
    with pytest.raises(ValueError):
        normalize_fingerprint("abc")


class _Response:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status, self._body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode()

    async def json(self, content_type: Optional[str] = None) -> Any:
        return json.loads(self._body)


class _Session:
    def __init__(self, answers: dict[str, _Response]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        return self.answers.get(url.split("47392", 1)[1], _Response(404, b'{"error": "not found"}'))


async def test_client_https_fingerprint_and_auth_probe() -> None:
    import aiohttp

    session = _Session({"/api/auth": _Response(200, b'{"ok": true, "writable": true, "admin": false}')})
    client = PclinkClient(session, "192.168.1.50", 47392, "secret", True, "AB:" * 31 + "AB")  # type: ignore[arg-type]
    assert client.base_url == "https://192.168.1.50:47392" and client.ws_url == "wss://192.168.1.50:47392/api/ws"
    assert await client.check_write_auth() == {"writable": True, "admin": False, "config_edit": False}
    method, url, kwargs = session.calls[-1]
    assert url == "https://192.168.1.50:47392/api/auth" and isinstance(kwargs["ssl"], aiohttp.Fingerprint)
    assert kwargs["ssl"].fingerprint == bytes.fromhex(FP) and len(session.calls) == 1      # config.yaml is not fetched
    # the read-only token is recognised as such
    session.answers["/api/auth"] = _Response(200, b'{"ok": true, "writable": false, "admin": false}')
    with pytest.raises(PclinkAuthError):
        await client.check_write_auth()
    session.answers["/api/auth"] = _Response(401, b"{}")
    with pytest.raises(PclinkAuthError):
        await client.check_write_auth()
    # a plain-HTTP client is asked exactly as before: no ssl argument at all
    plain_session = _Session({})
    plain = PclinkClient(plain_session, "192.168.1.50", 47392, "secret")  # type: ignore[arg-type]
    await plain.check_write_auth()   # an old service: /api/auth and /api/config both unknown -> inconclusive, accepted
    assert plain.base_url == "http://192.168.1.50:47392" and all("ssl" not in kwargs for _m, _u, kwargs in plain_session.calls)
    assert [url.rsplit("/", 1)[1] for _m, url, _k in plain_session.calls] == ["auth", "config"]


async def test_user_flow_accepts_a_pasted_url_and_https(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "two words", "port": 47392, "token": "secret"})
    assert result["errors"] == {"base": "invalid_host"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.50", "port": 47392, "token": "secret", "fingerprint": "nonsense"})
    assert result["errors"] == {"base": "invalid_fingerprint"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "https://192.168.1.50:47400/", "port": 47392, "token": "secret", "fingerprint": "AB:" * 31 + "AB"})
    await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "192.168.1.50", "port": 47400, "token": "secret", "tls": True, "fingerprint": FP}
    client = fake_client.instances[-1]
    assert client.tls and client.fingerprint == FP and client.port == 47400     # the entry's client uses them too


def _announcement(host: str, **props: str) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host), ip_addresses=[ip_address(host)], hostname="family-pc.local.",
        name="TestPC._pclink._tcp.local.", port=47392, type="_pclink._tcp.local.",
        properties={"id": "testpc", "name": "TestPC", "version": "0.4.0", "path": "/api", **props})


async def test_zeroconf_takes_https_from_the_announcement(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_announcement("192.168.1.50", tls="1", fp=FP))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "secret"})
    await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "192.168.1.50", "port": 47392, "token": "secret", "tls": True, "fingerprint": FP}


# -- 19: a known PC announces itself at a new address --------------------------------------------------
async def test_moved_pc_raises_a_repair_that_reconfigures_it(hass: HomeAssistant, fake_client, entry_data) -> None:
    from custom_components.pclink.repairs import async_create_fix_flow

    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data={**entry_data, "host": "10.0.0.9"})
    entry.add_to_hass(hass)                      # not loaded: the PC cannot be reached at 10.0.0.9
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_announcement("192.168.1.50"))
    assert result["type"] == data_entry_flow.FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["host"] == "10.0.0.9" and fake_client.instances == []       # still never repointed by an announcement
    found = issue(hass, "pc_moved", entry)
    assert found is not None and found.is_fixable and found.data["host"] == "192.168.1.50"
    assert found.translation_placeholders == {"name": "TestPC", "old": "10.0.0.9:47392", "new": "192.168.1.50:47392"}

    flow = await async_create_fix_flow(hass, found.issue_id, found.data)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == data_entry_flow.FlowResultType.FORM and form["step_id"] == "address"
    suggested = {str(key): (getattr(key, "description", None) or {}).get("suggested_value") for key in form["data_schema"].schema}
    assert suggested["host"] == "192.168.1.50" and suggested["token"] is None
    # the PC there must be the one the entry was made for
    orig_init = fake_client.__init__

    def other_pc_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self.health_doc["device"] = "someone-else"

    fake_client.__init__ = other_pc_init
    try:
        form = await flow.async_step_address({"host": "192.168.1.50", "port": 47392, "token": "secret"})
    finally:
        fake_client.__init__ = orig_init
    assert form["errors"] == {"base": "wrong_device"} and entry.data["host"] == "10.0.0.9"
    # the stored token does not go to an announced address by itself
    form = await flow.async_step_address({"host": "192.168.1.50", "port": 47392})
    assert form["errors"] == {"base": "token_required"}
    done = await flow.async_step_address({"host": "192.168.1.50", "port": 47392, "token": "secret"})
    await hass.async_block_till_done()
    assert done["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data == {"host": "192.168.1.50", "port": 47392, "token": "secret"}
    assert entry.state is ConfigEntryState.LOADED and hass.states.get("sensor.testpc_cpu_usage").state == "12.5"
    # an announcement elsewhere while the PC is connected is not worth an issue (several adapters) ...
    client = fake_client.instances[-1]
    ir.async_delete_issue(hass, DOMAIN, found.issue_id)
    hello(client)
    await hass.async_block_till_done()
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_announcement("192.168.1.77"))
    assert issue(hass, "pc_moved", entry) is None
    # ... and one raised earlier is cleared by the next connection
    ir.async_create_issue(hass, DOMAIN, f"pc_moved_{entry.entry_id}", is_fixable=True, severity=ir.IssueSeverity.WARNING,
                          translation_key="pc_moved", translation_placeholders={"name": "TestPC", "old": "a", "new": "b"})
    hello(client)
    await hass.async_block_till_done()
    assert issue(hass, "pc_moved", entry) is None


# -- 24 / C11: Wake-on-LAN -----------------------------------------------------------------------------
async def test_wake_uses_the_broadcast_address_the_pc_reports(hass: HomeAssistant, fake_client, entry_data) -> None:
    assert button_module.wake_targets("10.1.5.20", "10.1.7.255") == ["255.255.255.255", "10.1.7.255", "10.1.5.20"]
    assert button_module.wake_targets("10.1.5.20") == ["255.255.255.255", "10.1.5.255", "10.1.5.20"]      # an older service
    assert button_module.wake_targets("10.1.5.20", "not an address") == ["255.255.255.255", "10.1.5.255", "10.1.5.20"]
    assert button_module.wake_targets(None, "10.1.7.255") == ["255.255.255.255", "10.1.7.255"]
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    pc, users = make_state()
    hello(client)
    client.ws.push({"type": "state", "pc": {**pc, "mac": "AA:BB:CC:DD:EE:FF", "ip": "10.1.5.20", "broadcast": "10.1.7.255"}, "users": users})
    await hass.async_block_till_done()
    with patch.object(button_module, "send_magic_packet", return_value=6) as send:
        await hass.services.async_call("button", "press", {"entity_id": "button.testpc_wake_up"}, blocking=True)
    send.assert_called_once_with("AA:BB:CC:DD:EE:FF", "10.1.5.20", "10.1.7.255")
    assert hass.data[DOMAIN][entry.entry_id]._cache_data()["broadcast"] == "10.1.7.255"


# -- strings / scripts ---------------------------------------------------------------------------------
def test_strings_cover_actions_triggers_and_repairs() -> None:
    import yaml

    base = os.path.join(ROOT, "custom_components", "pclink")
    with open(os.path.join(base, "strings.json"), encoding="utf-8") as fh:
        strings = json.load(fh)
    with open(os.path.join(base, "translations", "en.json"), encoding="utf-8") as fh:
        assert json.load(fh) == strings
    with open(os.path.join(base, "services.yaml"), encoding="utf-8") as fh:
        services = yaml.safe_load(fh)
    from custom_components.pclink.const import PC_TRIGGERS, USER_TRIGGERS
    from custom_components.pclink.services import ALL_PCS_ACTIONS, SERVICES

    assert set(services) == set(strings["services"]) == set(SERVICES)
    for name, spec in services.items():
        assert set(spec["fields"]) == set(strings["services"][name]["fields"]), name
    assert ALL_PCS_ACTIONS <= set(SERVICES)
    assert set(strings["device_automation"]["trigger_type"]) == set(USER_TRIGGERS) | set(PC_TRIGGERS)
    assert set(strings["issues"]) == {"wrong_pc", "pc_moved"}
    for key in ("invalid_host", "invalid_fingerprint", "tls_error", "wrong_device", "invalid_auth", "cannot_connect"):
        assert key in strings["config"]["error"] and key in strings["issues"]["pc_moved"]["fix_flow"]["error"]
    for step in ("user", "reconfigure"):
        assert {"tls", "fingerprint"} <= set(strings["config"]["step"][step]["data"])


def test_sync_example_config_reports_a_missing_marker(tmp_path, capsys) -> None:
    spec = importlib.util.spec_from_file_location("sync_example_config", os.path.join(ROOT, "scripts", "sync_example_config.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yaml_file, py_file = tmp_path / "config.example.yaml", tmp_path / "example_config.py"
    yaml_file.write_text("device:\n  id: pc\n", encoding="utf-8")
    module.YAML, module.PY = str(yaml_file), str(py_file)
    py_file.write_text("EMBEDDED = r'''old'''\n", encoding="utf-8")
    assert module.main() == 0 and py_file.read_text(encoding="utf-8") == "EMBEDDED = r'''device:\n  id: pc\n'''\n"
    assert module.main() == 0 and "already in sync" in capsys.readouterr().out
    # the marker was reformatted: that is an error, not "in sync"
    py_file.write_text('EMBEDDED = """old"""\n', encoding="utf-8")
    assert module.main() == 1 and "nothing written" in capsys.readouterr().err
    assert py_file.read_text(encoding="utf-8") == 'EMBEDDED = """old"""\n'
    # a text that would break the raw string is refused
    py_file.write_text("EMBEDDED = r'''old'''\n", encoding="utf-8")
    yaml_file.write_text("path: C:\\", encoding="utf-8")
    assert module.main() == 1 and py_file.read_text(encoding="utf-8") == "EMBEDDED = r'''old'''\n"
