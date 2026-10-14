"""The "PC Link (all PCs)" roll-up device, worked out inside Home Assistant."""
from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pclink.const import DOMAIN, ROLLUP_UNIQUE_PREFIX
from custom_components.pclink.rollup import build_rollup, get_hub

HOSTS = {"192.168.1.50": ("testpc", "TestPC"), "192.168.1.51": ("den", "Den")}


def several_pcs(fake_client):
    """A client class for which every host is its own PC: its own id, name and entity ids."""

    class Client(fake_client):
        def __init__(self, session, host, port, token, *args, **kwargs) -> None:
            super().__init__(session, host, port, token, *args, **kwargs)
            pc_id, name = HOSTS[host]
            self.health_doc.update(device=pc_id, name=name)
            self.catalog = json.loads(json.dumps(self.catalog).replace("pclink_testpc_", f"pclink_{pc_id}_").replace("TestPC", name))
            self.user_state["kevin"].update(date=dt_util.now().date().isoformat(), used_minutes=30 if pc_id == "testpc" else 12.5,
                                            gaming_minutes=5, presence="active")
            if pc_id == "den":
                self.user_state["kevin"].update(status="idle", remaining_minutes=20)
                self.user_state["sarah"] = {"user": "sarah", "name": "Sarah", "status": "active", "logged_in": True,
                                            "used_minutes": 7, "date": dt_util.now().date().isoformat()}

    return patch("custom_components.pclink.PclinkClient", Client)


async def add_pc(hass: HomeAssistant, host: str) -> MockConfigEntry:
    pc_id, name = HOSTS[host]
    entry = MockConfigEntry(domain=DOMAIN, unique_id=pc_id, title=name, data={"host": host, "port": 47392, "token": "secret"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def rollup_entities(hass: HomeAssistant) -> dict[str, str]:
    """unique id (without the prefix) -> config entry id, for every roll-up entity in the registry."""
    registry = er.async_get(hass)
    return {e.unique_id[len(ROLLUP_UNIQUE_PREFIX):]: e.config_entry_id
            for entry in hass.config_entries.async_entries(DOMAIN)
            for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.unique_id.startswith(ROLLUP_UNIQUE_PREFIX)}


def test_build_rollup_adds_up_and_ignores_stale_or_bogus_figures() -> None:
    kevin = {"name": "Kevin", "date": "2026-09-28", "used_minutes": 30, "gaming_minutes": 10, "status": "idle",
             "presence": "idle", "logged_in": True, "managed": True, "remaining_minutes": 60}
    result = build_rollup([
        {"id": "a", "name": "Study", "online": True, "users": {"Kevin": kevin}},
        {"id": "b", "name": "Den", "online": True, "users": {"kevin": {**kevin, "used_minutes": 15, "status": "active",
                                                                         "presence": "active", "remaining_minutes": 45}}},
        {"id": "c", "name": "Attic", "online": False, "users": {"kevin": {**kevin, "used_minutes": 5, "status": "active"},
                                                                 "sarah": {"date": "2026-09-27", "used_minutes": 99}}},
        {"id": "d", "name": "Odd", "online": True, "users": {"kevin": {"used_minutes": float("nan"), "remaining_minutes": "x"},
                                                              "bad": "not a payload"}},
    ], "2026-09-28")
    user = result["users"]["kevin"]
    assert user["total_minutes_today"] == 50 and user["gaming_minutes"] == 30 and user["name"] == "Kevin"
    assert user["per_pc"] == {"Study": 30, "Den": 15, "Attic": 5, "Odd": 0}
    assert user["status"] == "active" and user["presence"] == "active" and user["remaining_minutes"] == 45
    assert user["active_on"] == ["Den"] and user["logged_in_on"] == ["Den", "Study"]   # the switched-off PC is not "active"
    assert "sarah" not in result["users"]                                               # yesterday's figures of a PC that is off
    assert result["pcs_online"] == 3 and result["pcs_in_use_list"] == ["Den"] and result["any_pc_in_use"] is True
    assert result["pcs"]["Attic"] == {"online": False, "users_active": [], "users_logged_in": []}
    assert build_rollup([], "2026-09-28")["any_pc_in_use"] is False


async def test_one_pc_has_no_rollup(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        await add_pc(hass, "192.168.1.50")
    assert rollup_entities(hass) == {}
    assert get_hub(hass).owner_entry_id is None


async def test_rollup_of_two_pcs(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        first = await add_pc(hass, "192.168.1.50")
        second = await add_pc(hass, "192.168.1.51")
        # the entry that completes the pair holds the entities
        entities = rollup_entities(hass)
        assert set(entities.values()) == {second.entry_id}
        assert {"pcs_online", "pcs_in_use", "any_pc_in_use", "user:kevin:total_minutes_today", "user:kevin:status",
                "user:kevin:active_on", "user:kevin:logged_in_on", "user:sarah:total_minutes_today"} <= set(entities)
        device = next((d for d in dr.async_entries_for_config_entry(dr.async_get(hass), second.entry_id)
                       if (DOMAIN, "all_pcs") in d.identifiers), None)
        assert device is not None and device.name == "PC Link (all PCs)"
        assert hass.states.get("sensor.pc_link_all_pcs_pcs_online").state == "2"
        assert hass.states.get("sensor.pc_link_all_pcs_pcs_in_use").attributes["pcs"] == ["Den", "TestPC"]
        assert hass.states.get("binary_sensor.pc_link_all_pcs_any_pc_in_use").state == "on"
        total = hass.states.get("sensor.pc_link_all_pcs_kevin_total_time_today")
        assert total.state == "42.5" and total.attributes["per_pc"] == {"TestPC": 30, "Den": 12.5}
        assert total.attributes["remaining_minutes"] == 20 and total.attributes["unit_of_measurement"] == "min"
        assert hass.states.get("sensor.pc_link_all_pcs_kevin_status_all_pcs").state == "active"
        assert hass.states.get("sensor.pc_link_all_pcs_kevin_active_on").state == "TestPC"
        assert hass.states.get("sensor.pc_link_all_pcs_sarah_active_on").state == "Den"
        assert hass.states.get("binary_sensor.pc_link_all_pcs_kevin_logged_in_anywhere").state == "on"
        # a push from one PC updates the roll-up; a user nobody knew gets entities
        den = next(c for c in fake_client.instances if c.host == "192.168.1.51")
        users = json.loads(json.dumps(den.user_state))
        users["kevin"]["used_minutes"] = 20
        users["tom"] = {"user": "tom", "name": "Tom", "status": "locked", "logged_in": True, "used_minutes": 3}
        den.ws.push({"type": "hello", "writable": True, "status": den.health_doc})
        den.ws.push({"type": "state", "pc": den.pc_state, "users": users})
        await hass.async_block_till_done()
        assert float(hass.states.get("sensor.pc_link_all_pcs_kevin_total_time_today").state) == 50
        assert hass.states.get("sensor.pc_link_all_pcs_tom_status_all_pcs").state == "locked"
        assert hass.states.get("sensor.pc_link_all_pcs_tom_active_on").state == "none"
        # the PC's own catalog does not list these entities; that must never get them removed
        coordinator = hass.data[DOMAIN][second.entry_id]
        coordinator._missing.clear()
        coordinator._reconcile_registry()
        assert not any(uid.startswith(ROLLUP_UNIQUE_PREFIX) for uid in coordinator._missing)
        # the holder is deleted: the remaining PC alone needs no roll-up
        assert await hass.config_entries.async_remove(second.entry_id)
        await hass.async_block_till_done()
        assert rollup_entities(hass) == {} and get_hub(hass).owner_entry_id is None
        assert hass.states.get("sensor.testpc_cpu_usage").state == "12.5"
        assert first.entry_id in hass.data[DOMAIN]


async def test_rollup_moves_to_another_pc_when_its_holder_is_deleted(hass: HomeAssistant, fake_client, entry_data) -> None:
    HOSTS["192.168.1.52"] = ("attic", "Attic")
    try:
        with several_pcs(fake_client):
            first = await add_pc(hass, "192.168.1.50")
            second = await add_pc(hass, "192.168.1.51")
            third = await add_pc(hass, "192.168.1.52")
            assert set(rollup_entities(hass).values()) == {second.entry_id}
            assert hass.states.get("sensor.pc_link_all_pcs_pcs_online").state == "3"
            assert await hass.config_entries.async_remove(second.entry_id)
            await hass.async_block_till_done()
            holder = get_hub(hass).owner_entry_id
            assert holder in (first.entry_id, third.entry_id)
            assert set(rollup_entities(hass).values()) == {holder}
            assert hass.states.get("sensor.pc_link_all_pcs_pcs_online").state == "2"
            assert hass.states.get("sensor.pc_link_all_pcs_kevin_total_time_today").state == "42.5"   # TestPC 30 + Attic 12.5
            # a reload of the holder keeps the roll-up with it
            assert await hass.config_entries.async_reload(holder)
            await hass.async_block_till_done()
            assert get_hub(hass).owner_entry_id == holder
            assert hass.states.get("sensor.pc_link_all_pcs_pcs_online").state == "2"
    finally:
        HOSTS.pop("192.168.1.52", None)


async def test_shared_budget_pcs_are_told_the_usage_of_the_others(hass: HomeAssistant, fake_client, entry_data) -> None:
    from custom_components.pclink import rollup

    def sent(client):
        return [c for c in client.commands if c.get("action") == "peer_usage"]

    with several_pcs(fake_client):
        await add_pc(hass, "192.168.1.50")
        await add_pc(hass, "192.168.1.51")
        testpc = next(c for c in fake_client.instances if c.host == "192.168.1.50")
        den = next(c for c in fake_client.instances if c.host == "192.168.1.51")
        assert sent(testpc) == [] and sent(den) == []            # neither PC keeps a shared budget
        # Den switches it on: it gets what its users did on the other PC, and only Den does
        den_pc = {**den.pc_state, "shared_budget": True}
        den.ws.push({"type": "hello", "writable": True, "status": den.health_doc})
        den.ws.push({"type": "state", "pc": den_pc, "users": den.user_state})
        await hass.async_block_till_done()
        today = den.user_state["kevin"]["date"]
        assert sent(den) == [{"action": "peer_usage", "peers": {"testpc": {"name": "TestPC", "users": {
            "kevin": {"name": "kevin", "date": today, "used_minutes": 30}}}}}]
        assert sent(testpc) == []
        # more usage on the other PC: not passed on at once (at most every PEER_PUSH_SECONDS) ...
        users = json.loads(json.dumps(testpc.user_state))
        users["kevin"]["used_minutes"] = 31
        testpc.ws.push({"type": "hello", "writable": True, "status": testpc.health_doc})
        testpc.ws.push({"type": "state", "pc": testpc.pc_state, "users": users})
        await hass.async_block_till_done()
        assert len(sent(den)) == 1
        # ... but a new connection of Den (it may have restarted) gets the figures right away
        den.ws.push({"type": "hello", "writable": True, "status": den.health_doc})
        await hass.async_block_till_done()
        assert len(sent(den)) == 2 and sent(den)[-1]["peers"]["testpc"]["users"]["kevin"]["used_minutes"] == 31
        with patch.object(rollup, "PEER_PUSH_SECONDS", 0):
            users["kevin"]["used_minutes"] = 32
            testpc.ws.push({"type": "state", "pc": testpc.pc_state, "users": users})
            await hass.async_block_till_done()
        assert len(sent(den)) == 3 and sent(den)[-1]["peers"]["testpc"]["users"]["kevin"]["used_minutes"] == 32
        assert sent(testpc) == []


def test_peer_usage_leaves_out_what_has_no_day() -> None:
    from custom_components.pclink.rollup import peer_usage

    assert peer_usage({"kevin": {"date": "2026-09-28", "used_minutes": 5, "history": [], "status": "active", "rules": {}},
                       "sarah": {"used_minutes": 9}, "bad": "x"}) == {"kevin": {"date": "2026-09-28", "used_minutes": 5, "history": []}}
    assert peer_usage({}) == {} and peer_usage(None) == {}
