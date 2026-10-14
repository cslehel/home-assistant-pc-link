"""Tests for the fixes of the second 2026-10 review of the Home Assistant integration: the
token and announced addresses, stale values on reconnect, unload order, the roll-up's day,
HTTP 429, peer-usage refusals, several PCs in one action, admin-only actions, the recorder."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from ipaddress import ip_address
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import MATCH_ALL
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import area_registry as ar, device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pclink.api import PclinkBusyError, PclinkClient, PclinkThrottledError
from custom_components.pclink.const import DOMAIN
from custom_components.pclink.rollup import RollupHub, build_rollup

from conftest import make_catalog
from test_rollup import add_pc, several_pcs

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2024.11
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FP = "ab" * 32
OTHER_FP = "cd" * 32


async def setup_entry(hass: HomeAssistant, entry_data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _announcement(host: str, **props: str) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host), ip_addresses=[ip_address(host)], hostname="family-pc.local.",
        name="TestPC._pclink._tcp.local.", port=47392, type="_pclink._tcp.local.",
        properties={"id": "testpc", "name": "TestPC", "version": "0.4.0", "path": "/api", **props})


def _suggested(form) -> dict:
    return {str(key): (getattr(key, "description", None) or {}).get("suggested_value") for key in form["data_schema"].schema}


# -- 1: the "PC moved" repair -----------------------------------------------------------------------
async def test_moved_repair_keeps_the_entrys_certificate_and_asks_for_the_token(hass: HomeAssistant, fake_client, entry_data) -> None:
    from custom_components.pclink.repairs import async_create_fix_flow

    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC",
                            data={**entry_data, "host": "10.0.0.9", "tls": True, "fingerprint": FP})
    entry.add_to_hass(hass)                      # not loaded: unreachable at 10.0.0.9
    # whoever announces "testpc" elsewhere says: plain HTTP (or another certificate)
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF},
                                              data=_announcement("192.168.1.66", tls="1", fp=OTHER_FP))
    found = ir.async_get(hass).async_get_issue(DOMAIN, f"pc_moved_{entry.entry_id}")
    assert found is not None and found.data == {"entry_id": entry.entry_id, "host": "192.168.1.66", "port": 47392}

    flow = await async_create_fix_flow(hass, found.issue_id, found.data)
    flow.hass = hass
    form = await flow.async_step_init()
    shown = _suggested(form)
    assert shown["host"] == "192.168.1.66" and shown["fingerprint"] == FP and shown["tls"] is True and shown["token"] is None
    assert FP in form["description_placeholders"]["security"]
    # the announced address without the pinned certificate: the stored token is not sent there
    form = await flow.async_step_address({"host": "192.168.1.66", "port": 47392, "tls": True})
    assert form["errors"] == {"base": "token_required"} and fake_client.instances == []
    form = await flow.async_step_address({"host": "192.168.1.66", "port": 47392, "tls": True, "fingerprint": OTHER_FP})
    assert form["errors"] == {"base": "token_required"} and fake_client.instances == []
    # with the pinned certificate kept only the real PC can answer: the stored token may go
    done = await flow.async_step_address({"host": "192.168.1.66", "port": 47392, "tls": True, "fingerprint": FP})
    await hass.async_block_till_done()
    assert done["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data == {"host": "192.168.1.66", "port": 47392, "tls": True, "fingerprint": FP, "token": "secret"}
    assert fake_client.instances[0].fingerprint == FP


async def test_an_old_moved_issue_does_not_bring_its_certificate_along(hass: HomeAssistant, fake_client, entry_data) -> None:
    from custom_components.pclink.repairs import async_create_fix_flow

    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data={**entry_data, "host": "10.0.0.9"})
    entry.add_to_hass(hass)
    # raised by an older version, which stored what the announcement said about HTTPS
    flow = await async_create_fix_flow(hass, "pc_moved_x", {"entry_id": entry.entry_id, "host": "192.168.1.66", "port": 47392,
                                                            "tls": True, "fingerprint": OTHER_FP})
    flow.hass = hass
    form = await flow.async_step_init()
    shown = _suggested(form)
    assert shown["host"] == "192.168.1.66" and shown["fingerprint"] is None and not shown["tls"]
    assert "not encrypted" in form["description_placeholders"]["security"]


# -- 2: what an announcement says about HTTPS is shown ----------------------------------------------
async def test_zeroconf_confirm_shows_the_announced_certificate(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_announcement("192.168.1.50", tls="1", fp=FP))
    assert result["step_id"] == "zeroconf_confirm" and FP in result["description_placeholders"]["security"]
    hass.config_entries.flow.async_abort(result["flow_id"])
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_announcement("192.168.1.50"))
    assert "HTTP, not encrypted" in result["description_placeholders"]["security"]
    strings = json.load(open(os.path.join(ROOT, "custom_components", "pclink", "strings.json"), encoding="utf-8"))
    assert "{security}" in strings["config"]["step"]["zeroconf_confirm"]["description"]
    for step in (strings["config"]["step"]["reconfigure"], strings["issues"]["pc_moved"]["fix_flow"]["step"]["address"],
                 strings["issues"]["wrong_pc"]["fix_flow"]["step"]["address"]):
        assert "{security}" in step["description"]
    for errors in (strings["config"]["error"], strings["issues"]["pc_moved"]["fix_flow"]["error"]):
        assert {"token_required", "too_many_attempts"} <= set(errors)


# -- 3 / 4: adding a known PC again; the token is never offered back --------------------------------
async def test_adding_a_pc_again_over_http_drops_its_old_certificate(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data={**entry_data, "tls": True, "fingerprint": FP})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.50", "port": 47392, "token": "secret"})
    await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.ABORT and result["reason"] == "already_configured"
    assert not entry.data.get("tls") and not entry.data.get("fingerprint") and entry.data["token"] == "secret"


async def test_user_step_does_not_echo_the_token(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.50", "port": 47392, "token": "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    shown = _suggested(result)
    assert shown["host"] == "192.168.1.50" and shown["token"] is None


# -- 5: no stale values on reconnect -----------------------------------------------------------------
async def test_hello_alone_does_not_bring_back_the_old_values(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert hass.states.get("sensor.testpc_cpu_usage").state == "12.5"
    coordinator.last_update_success = False      # the PC went away (the poll failed)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.testpc_cpu_usage").state == "unavailable"
    coordinator._handle_message({"type": "hello", "writable": True, "status": client.health_doc})
    await hass.async_block_till_done()
    assert not coordinator.ws_connected and hass.states.get("sensor.testpc_cpu_usage").state == "unavailable"
    coordinator._handle_message({"type": "state", "pc": {**client.pc_state, "cpu_percent": 77}, "users": client.user_state})
    await hass.async_block_till_done()
    assert coordinator.ws_connected and hass.states.get("sensor.testpc_cpu_usage").state == "77"


# -- 6: a "wrong PC" issue fixed by Reconfigure goes away ---------------------------------------------
async def test_wrong_pc_issue_is_cleared_by_the_first_good_answer(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    ir.async_create_issue(hass, DOMAIN, f"wrong_pc_{entry.entry_id}", is_fixable=True, severity=ir.IssueSeverity.ERROR,
                          translation_key="wrong_pc", translation_placeholders={"name": "TestPC", "found_name": "Den", "address": "x"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"wrong_pc_{entry.entry_id}") is None


# -- 7: unload stops the connection before the platforms go -------------------------------------------
async def test_unload_stops_the_pc_before_its_platforms(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    seen = []
    original = hass.config_entries.async_unload_platforms

    async def unload_platforms(entry_, platforms):
        seen.append(coordinator.stopped)
        # a catalog with a new entity arriving now must not reach the platforms being torn down
        catalog = make_catalog()
        catalog["entities"].append({"unique_id": "pclink_testpc_late", "key": "late", "platform": "sensor", "name": "Late", "value": "x"})
        coordinator._handle_message_safely({"type": "entities", **catalog})
        coordinator._apply_catalog(catalog)
        return await original(entry_, platforms)

    with patch.object(hass.config_entries, "async_unload_platforms", unload_platforms):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert seen == [True] and "late" not in coordinator.live and "late" not in coordinator.catalog_keys


# -- 8: the roll-up counts today's minutes only -------------------------------------------------------
def test_rollup_counts_only_todays_minutes_of_an_online_pc() -> None:
    yesterday = {"name": "Kevin", "date": "2026-09-27", "used_minutes": 80, "gaming_minutes": 40, "status": "active",
                 "logged_in": True, "presence": "active"}
    result = build_rollup([
        {"id": "a", "name": "Study", "online": True, "users": {"kevin": yesterday}},
        {"id": "b", "name": "Den", "online": True, "users": {"kevin": {**yesterday, "date": "2026-09-28", "used_minutes": 10,
                                                                         "gaming_minutes": 0, "status": "idle"}}},
        {"id": "c", "name": "Old", "online": True, "users": {"kevin": {"used_minutes": 5, "status": "idle"}}},   # no date: an old service
    ], "2026-09-28")
    user = result["users"]["kevin"]
    assert user["total_minutes_today"] == 15 and user["gaming_minutes"] == 0
    assert user["per_pc"] == {"Den": 10, "Old": 5}
    assert user["status"] == "active" and user["active_on"] == ["Study"]          # still where it is in use


# -- 9: HTTP 429 -------------------------------------------------------------------------------------
async def test_429_is_a_throttled_error_with_retry_after(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get("http://192.168.1.50:47392/api/health", status=429, headers={"Retry-After": "42"},
                       json={"error": "too many wrong tokens; try again in 42 s"})
    client = PclinkClient(async_get_clientsession(hass), "192.168.1.50", 47392, "wrong")
    with pytest.raises(PclinkThrottledError) as caught:
        await client.health()
    assert caught.value.retry_after == 42
    # no connection free for token holders: 503 with Retry-After
    aioclient_mock.get("http://192.168.1.50:47392/api/status", status=503, headers={"Retry-After": "5"}, json={"error": "busy"})
    with pytest.raises(PclinkBusyError) as caught:
        await client.status()
    assert caught.value.retry_after == 5


async def test_a_throttled_socket_request_falls_back_to_polling(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client.ws.push({"type": "hello", "writable": True, "status": client.health_doc})
    client.ws.push({"type": "state", "pc": client.pc_state, "users": client.user_state})
    client.ws.push({"type": "status", "id": 11, "ok": False, "error": "too many requests; try again in 30 s", "retry_after": 30})
    await hass.async_block_till_done()
    assert coordinator.ws_connected and hass.states.get("sensor.testpc_cpu_usage").state == "12.5"   # not taken as a state
    asked = len(client.ws.sent)
    with patch.object(coordinator, "async_request_refresh") as refresh:
        await coordinator.async_request_state()
    assert len(client.ws.sent) == asked and refresh.called


async def test_flows_say_when_the_pc_throttles(hass: HomeAssistant, fake_client) -> None:
    async def throttled(self):
        raise PclinkThrottledError("too many", 30)

    async def busy(self):
        raise PclinkBusyError("busy", 5)

    with patch.object(fake_client, "health", throttled):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.50", "port": 47392, "token": "secret"})
    assert result["errors"] == {"base": "too_many_attempts"}
    with patch.object(fake_client, "health", busy):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.50", "port": 47392, "token": "secret"})
    assert result["errors"] == {"base": "busy"}


async def test_no_connection_attempts_while_a_new_token_is_asked_for(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert not coordinator._reauth_in_progress()
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    assert coordinator._reauth_in_progress()
    connects = client.ws_connects
    coordinator._backoff = 0.01
    await client.ws.close()                      # the socket drops: the loop would reconnect
    for _ in range(20):
        await asyncio.sleep(0.02)
    assert client.ws_connects == connects


async def test_reconnect_backoff_has_jitter(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    waits: list[float] = []

    async def wait(seconds):
        waits.append(seconds)

    with patch.object(coordinator, "_wait", wait):
        for _ in range(30):
            coordinator._backoff = 10
            await coordinator._pause()
    assert all(8 <= w <= 12 for w in waits) and len({round(w, 3) for w in waits}) > 1


# -- 11: a refused peer_usage backs off --------------------------------------------------------------
async def test_refused_peer_usage_backs_off_and_warns_once(hass: HomeAssistant, caplog) -> None:
    hub = RollupHub(hass)
    sent = []

    async def send(data):
        sent.append(data)
        return {"ok": False, "error": "unknown action"}

    target = SimpleNamespace(pc_id="den", pc_name="Den", pc={"shared_budget": True}, online=True, users={}, connections=1,
                             entry=SimpleNamespace(entry_id="e1"), async_send=send)
    hub._coordinators["e1"] = target
    caplog.set_level(logging.DEBUG, logger="custom_components.pclink.rollup")
    await hub._send_peer_usage("e1", target, {})
    await hub._send_peer_usage("e1", target, {})
    assert hub._peer_retry["e1"][0] == 2
    # a throttled refusal is not tried again before the PC's time is up
    async def throttled(data):
        return {"ok": False, "error": "try again in 900 s", "retry_after": 900}

    target.async_send = throttled
    before = time.monotonic()
    await hub._send_peer_usage("e1", target, {})
    hub.stop()
    assert hub._peer_retry["e1"][1] >= before + 900
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "did not take" in r.getMessage()]
    assert len(warnings) == 1


# -- 12: several PCs in one action; floors and labels -------------------------------------------------
async def test_one_failing_pc_does_not_cost_the_others_their_response(hass: HomeAssistant, fake_client) -> None:
    with several_pcs(fake_client):
        first = await add_pc(hass, "192.168.1.50")
        second = await add_pc(hass, "192.168.1.51")
    devices = dr.async_get(hass)
    ids = [d.id for entry in (first, second) for d in dr.async_entries_for_config_entry(devices, entry.entry_id)
           if (DOMAIN, "all_pcs") not in d.identifiers]
    den = next(c for c in fake_client.instances if c.host == "192.168.1.51")
    den.refuse = "no such user"
    response = await hass.services.async_call(DOMAIN, "usage_report", {"device_id": ids, "user": "kevin"},
                                              blocking=True, return_response=True)
    assert set(response["results"]) == {"testpc"} and set(response["errors"]) == {"den"}
    assert "no such user" in response["errors"]["den"]
    with pytest.raises(HomeAssistantError, match="no such user"):   # without a response the failure is raised
        await hass.services.async_call(DOMAIN, "lock", {"device_id": ids}, blocking=True)
    testpc = next(c for c in fake_client.instances if c.host == "192.168.1.50")
    testpc.refuse = "also no"
    with pytest.raises(HomeAssistantError):                          # all failed: raised with a response too
        await hass.services.async_call(DOMAIN, "usage_report", {"device_id": ids, "user": "kevin"}, blocking=True, return_response=True)


async def test_target_by_floor_and_label(hass: HomeAssistant, fake_client, entry_data) -> None:
    pytest.importorskip("homeassistant.helpers.floor_registry")
    from homeassistant.helpers import floor_registry as fr, label_registry as lr

    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) if (DOMAIN, "testpc") in d.identifiers)
    floor = fr.async_get(hass).async_create("Upstairs")
    area = ar.async_get(hass).async_create("Kids room")
    ar.async_get(hass).async_update(area.id, floor_id=floor.floor_id)
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    label = lr.async_get(hass).async_create("Kids PCs")
    dr.async_get(hass).async_update_device(device.id, labels={label.label_id})
    await hass.services.async_call(DOMAIN, "lock", {"floor_id": floor.floor_id}, blocking=True)
    await hass.services.async_call(DOMAIN, "logoff", {"label_id": [label.label_id]}, blocking=True)
    assert client.commands == [{"action": "lock"}, {"action": "logoff"}]
    with pytest.raises(ServiceValidationError, match="floor"):
        await hass.services.async_call(DOMAIN, "lock", {"floor_id": "nowhere"}, blocking=True)
    with pytest.raises(ServiceValidationError, match="label"):
        await hass.services.async_call(DOMAIN, "lock", {"label_id": "nothing"}, blocking=True)


# -- 13: actions that run programs are for administrators ---------------------------------------------
async def test_running_programs_needs_an_administrator(hass: HomeAssistant, fake_client, entry_data, hass_read_only_user) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    user = Context(user_id=hass_read_only_user.id)
    for name, data in (("command", {"data": {"action": "launch", "command": "calc.exe"}}),
                       ("launch", {"command": "calc.exe"}), ("reload_config", {})):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(DOMAIN, name, data, blocking=True, context=user)
    assert client.commands == []
    await hass.services.async_call(DOMAIN, "add_minutes", {"user": "kevin", "minutes": 5}, blocking=True, context=user)
    await hass.services.async_call(DOMAIN, "launch", {"command": "calc.exe"}, blocking=True)   # an automation: no user
    assert [c["action"] for c in client.commands] == ["add_minutes", "launch"]


# -- 14: a text that was refused is not kept -----------------------------------------------------------
async def test_refused_text_is_not_shown(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    client.refuse = "no agent session"
    with pytest.raises(HomeAssistantError, match="no agent session"):
        await hass.services.async_call("text", "set_value", {"entity_id": "text.testpc_kevin_message", "value": "Dinner"}, blocking=True)
    assert hass.states.get("text.testpc_kevin_message").state in ("", "unknown")
    client.refuse = None
    await hass.services.async_call("text", "set_value", {"entity_id": "text.testpc_kevin_message", "value": "Dinner"}, blocking=True)
    assert hass.states.get("text.testpc_kevin_message").state == "Dinner"


# -- 15: volatile attributes stay out of the recorder ---------------------------------------------------
async def test_volatile_attributes_are_not_recorded(hass: HomeAssistant, fake_client, entry_data) -> None:
    pc = make_catalog()
    for spec in pc["entities"]:
        if spec["key"] == "user_kevin_time_remaining":
            spec["unrecorded_attributes"] = ["limit_minutes"]
    with patch("conftest.make_catalog", return_value=pc):
        entry = await setup_entry(hass, entry_data)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sessions = coordinator.live["active_sessions"]            # no field from the PC: the built-in list (sessions)
    remaining = coordinator.live["user_kevin_time_remaining"]  # listed by the PC
    cpu = coordinator.live["cpu_percent"]

    def unrecorded(entity) -> frozenset:
        """What the recorder is told (``_state_info`` on current releases), checked to match the entity's own set."""
        own = entity._Entity__combined_unrecorded_attributes
        info = getattr(entity, "_state_info", None)
        if isinstance(info, dict) and "unrecorded_attributes" in info:
            assert info["unrecorded_attributes"] == own
        return own

    assert MATCH_ALL in unrecorded(sessions)
    assert "limit_minutes" in unrecorded(remaining) and MATCH_ALL not in unrecorded(remaining)
    assert MATCH_ALL not in unrecorded(cpu)
    # the platform's own exclusions are kept (sensor: options)
    assert "options" in unrecorded(coordinator.live["user_kevin_status"])
    # a changed catalog entry changes it at runtime
    cpu.async_update_spec(dict(cpu.spec, unrecorded_attributes=["by_cpu"]))
    assert "by_cpu" in unrecorded(cpu)


# -- 16: set_limit / rule_limit "unlimited" --------------------------------------------------------------
async def test_unlimited_limits(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    call = hass.services.async_call
    await call(DOMAIN, "set_limit", {"user": "kevin", "scope": "today", "minutes": "unlimited"}, blocking=True)
    await call(DOMAIN, "set_limit", {"user": "kevin", "scope": "weekly", "minutes": "Unlimited"}, blocking=True)
    await call(DOMAIN, "set_limit", {"user": "kevin", "scope": "weekend"}, blocking=True)
    await call(DOMAIN, "rule_limit", {"user": "kevin", "rule": "YouTube", "minutes": "unlimited"}, blocking=True)
    await call(DOMAIN, "rule_limit", {"user": "kevin", "rule": "YouTube", "minutes": "unlimited", "scope": "weekly"}, blocking=True)
    assert [c["minutes"] for c in client.commands] == [-1, 0, None, -1, 0]
    with pytest.raises(vol.Invalid):
        await call(DOMAIN, "set_limit", {"user": "kevin", "scope": "today", "minutes": "lots"}, blocking=True)
    with pytest.raises(vol.Invalid):
        await call(DOMAIN, "set_limit", {"user": "kevin", "scope": "today", "minutes": -5}, blocking=True)
