"""Phase-2 features on the Home Assistant side: profiles, time requests, shared bonus minutes,
and the tamper / service-health entities the PC's catalog now carries."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pclink.const import DOMAIN
from custom_components.pclink.diagnostics import async_get_config_entry_diagnostics
from custom_components.pclink.rollup import peer_usage

from conftest import make_catalog
from test_rollup import add_pc, settle, several_pcs

TAMPER_REASONS = ["agent_silent", "agent_killed", "report_rejected", "clock_changed", "time_zone_changed"]


async def setup_entry(hass: HomeAssistant, entry_data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _user(key: str, platform: str, name: str, **spec) -> dict:
    return {"unique_id": f"pclink_testpc_user_kevin_{key}", "key": f"user_kevin_{key}", "platform": platform,
            "name": f"kevin {name}", "source": "user", "user": "kevin", **spec}


def _pc(key: str, platform: str, name: str, **spec) -> dict:
    return {"unique_id": f"pclink_testpc_{key}", "key": key, "platform": platform, "name": name, "source": "pc", **spec}


# -- F2: profiles ----------------------------------------------------------------------------------
async def test_set_profile_action(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    call = hass.services.async_call
    await call(DOMAIN, "set_profile", {"user": "kevin", "profile": " holiday ", "pin": "1234"}, blocking=True)
    await call(DOMAIN, "set_profile", {"profile": "normal"}, blocking=True)       # no user: everyone who has it
    assert client.commands == [{"action": "set_profile", "user": "kevin", "profile": "holiday", "pin": "1234"},
                               {"action": "set_profile", "profile": "normal"}]
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        await call(DOMAIN, "set_profile", {"user": "kevin", "profile": "  "}, blocking=True)


async def test_set_profile_goes_to_every_pc_through_the_all_pcs_device(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        await add_pc(hass, "192.168.1.50")
        second = await add_pc(hass, "192.168.1.51")
        rollup = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), second.entry_id)
                      if (DOMAIN, "all_pcs") in d.identifiers)
        await hass.services.async_call(DOMAIN, "set_profile", {"device_id": rollup.id, "profile": "holiday"}, blocking=True)
        assert [c.commands[-1] for c in fake_client.instances] == [{"action": "set_profile", "profile": "holiday"}] * 2
        # an answer to a time request belongs to the PC it was made on
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, "answer_time_request", {"device_id": rollup.id, "user": "kevin", "approve": True}, blocking=True)


async def test_profile_select_follows_the_catalog_and_shows_an_unlisted_profile(hass: HomeAssistant, fake_client, entry_data) -> None:
    await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    catalog = make_catalog()
    catalog["entities"].append(_user("profile", "select", "profile", value="profile", options=["normal", "holiday"],
                                     category="config", command="user_kevin_profile"))
    users = json.loads(json.dumps(client.user_state))
    users["kevin"].update(profile="normal", profiles=["holiday"])
    client.ws.push({"type": "hello", "writable": True, "status": client.health_doc})
    client.ws.push({"type": "entities", **catalog})
    client.ws.push({"type": "state", "pc": client.pc_state, "users": users})
    await hass.async_block_till_done()
    entity_id = "select.testpc_kevin_profile"
    assert hass.states.get(entity_id).state == "normal" and hass.states.get(entity_id).attributes["options"] == ["normal", "holiday"]
    await hass.services.async_call("select", "select_option", {"entity_id": entity_id, "option": "holiday"}, blocking=True)
    assert client.commands[-1] == {"action": "set", "entity": "user_kevin_profile", "value": "holiday"}
    # The PC's configuration is reloaded with a new profile and it is switched to at once:
    # the state may arrive before the catalog.  The value is shown, not "unknown".
    users["kevin"].update(profile="exams", profiles=["exams", "holiday"])
    client.ws.push({"type": "state", "pc": client.pc_state, "users": users})
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "exams" and state.attributes["options"] == ["normal", "holiday", "exams"]
    catalog["entities"][-1] = {**catalog["entities"][-1], "options": ["normal", "exams", "holiday"]}
    client.ws.push({"type": "entities", **catalog})
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "exams" and state.attributes["options"] == ["normal", "exams", "holiday"]
    await hass.services.async_call("select", "select_option", {"entity_id": entity_id, "option": "normal"}, blocking=True)
    assert client.commands[-1]["value"] == "normal"
    # nothing reported: unknown, with the listed options
    users["kevin"].pop("profile")
    client.ws.push({"type": "state", "pc": client.pc_state, "users": users})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"


# -- F3: what was granted on the other PCs ---------------------------------------------------------
def test_peer_usage_passes_local_bonus_and_bedtime_extension() -> None:
    users = {"kevin": {"name": "Kevin", "date": "2026-10-13", "used_minutes": 40, "bonus_local_minutes": 15.0,
                       "bedtime_extension_local_minutes": 30, "bonus_minutes": 45, "bedtime_extension_minutes": 60, "status": "active"},
             "sarah": {"date": "2026-10-13", "used_minutes": 5}}
    assert peer_usage(users) == {
        "kevin": {"name": "Kevin", "date": "2026-10-13", "used_minutes": 40, "bonus_local_minutes": 15.0,
                  "bedtime_extension_local_minutes": 30},        # never the totals that include what others granted
        "sarah": {"date": "2026-10-13", "used_minutes": 5}}


async def test_shared_budget_pc_is_told_the_bonus_granted_elsewhere(hass: HomeAssistant, fake_client, entry_data) -> None:
    with several_pcs(fake_client):
        await add_pc(hass, "192.168.1.50")
        await add_pc(hass, "192.168.1.51")
        testpc = next(c for c in fake_client.instances if c.host == "192.168.1.50")
        den = next(c for c in fake_client.instances if c.host == "192.168.1.51")
        users = json.loads(json.dumps(testpc.user_state))
        users["kevin"].update(bonus_local_minutes=20, bedtime_extension_local_minutes=15)
        testpc.ws.push({"type": "hello", "writable": True, "status": testpc.health_doc})
        testpc.ws.push({"type": "state", "pc": testpc.pc_state, "users": users})
        den.ws.push({"type": "hello", "writable": True, "status": den.health_doc})
        den.ws.push({"type": "state", "pc": {**den.pc_state, "shared_budget": True}, "users": den.user_state})
        await settle(hass)
        sent = [c for c in den.commands if c.get("action") == "peer_usage"][-1]["peers"]["testpc"]["users"]["kevin"]
        assert sent["bonus_local_minutes"] == 20 and sent["bedtime_extension_local_minutes"] == 15 and sent["date"]
        # ... and it is remembered for a PC that is switched off when Home Assistant restarts
        stored = hass.data[DOMAIN][next(e.entry_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == "testpc")]._cache_data()
        assert stored["users"]["kevin"]["bonus_local_minutes"] == 20


# -- the entities CORE's catalog gained in phase 1 and 2 -------------------------------------------
async def test_tamper_health_and_time_request_entities_render(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    catalog = make_catalog()
    catalog["entities"] += [
        _pc("degraded", "binary_sensor", "Service problem", value="degraded", device_class="problem", category="diagnostic"),
        _pc("tick_failures", "sensor", "Service errors", value="tick_failures", state_class="total_increasing",
            category="diagnostic", attributes="health", icon="mdi:alert-circle-outline"),
        _pc("last_good_tick", "sensor", "Last complete check", value="last_good_tick", device_class="timestamp", category="diagnostic"),
        _user("tamper", "binary_sensor", "tampering suspected", value="tamper", device_class="tamper", icon="mdi:shield-alert"),
        _user("tamper_reason", "sensor", "tampering reason", value="tamper_reason", device_class="enum", options=TAMPER_REASONS,
              attributes="tamper_details", category="diagnostic"),
        _user("clear_tamper", "button", "clear tampering warning", category="config", command="user_kevin_clear_tamper"),
        _user("login_blocked", "binary_sensor", "login blocked", value="login_blocked", icon="mdi:account-lock"),   # no device class any more
        _user("time_request", "sensor", "time request", value="time_request_minutes", unit="min", device_class="duration",
              attributes="time_request"),
        _user("approve_time_request", "button", "approve time request", command="user_kevin_approve_time_request"),
    ]
    pc = {**client.pc_state, "degraded": False, "tick_failures": 0, "last_good_tick": "2026-10-13T18:00:05+02:00",
          "health": {"degraded": False, "tick_failures": 0, "last_error": None, "last_good_tick": "2026-10-13T18:00:05+02:00"}}
    users = json.loads(json.dumps(client.user_state))
    users["kevin"].update(tamper=False, tamper_reason=None, tamper_at=None, tamper_details={}, login_blocked=True,
                          time_request=None, time_request_minutes=0)
    client.ws.push({"type": "hello", "writable": True, "status": client.health_doc})
    client.ws.push({"type": "entities", **catalog})
    client.ws.push({"type": "state", "pc": pc, "users": users})
    await hass.async_block_till_done()
    get = hass.states.get
    assert get("binary_sensor.testpc_service_problem").state == "off"
    assert get("binary_sensor.testpc_service_problem").attributes["device_class"] == "problem"
    errors = get("sensor.testpc_service_errors")
    assert errors.state == "0" and errors.attributes["state_class"] == "total_increasing" and errors.attributes["last_error"] is None
    assert get("sensor.testpc_last_complete_check").state == "2026-10-13T16:00:05+00:00"       # a real time stamp, in UTC
    assert get("binary_sensor.testpc_kevin_tampering_suspected").state == "off"
    assert get("binary_sensor.testpc_kevin_tampering_suspected").attributes["device_class"] == "tamper"
    assert get("sensor.testpc_kevin_tampering_reason").state == "unknown"                       # no reason while all is well
    blocked = get("binary_sensor.testpc_kevin_login_blocked")
    assert blocked.state == "on" and "device_class" not in blocked.attributes                    # "on" reads as on, not "unlocked"
    assert get("sensor.testpc_kevin_time_request").state == "0"
    registry = er.async_get(hass)
    assert registry.async_get("sensor.testpc_service_errors").entity_category == "diagnostic"
    assert registry.async_get("button.testpc_kevin_clear_tampering_warning").entity_category == "config"

    # trouble: the agent was killed, the service has failing checks, the child asks for time
    pc.update(degraded=True, tick_failures=3, health={"degraded": True, "tick_failures": 3, "last_error": "sqlite secret-path is locked",
                                                      "last_good_tick": "2026-10-13T18:00:05+02:00"})
    users["kevin"].update(tamper=True, tamper_reason="agent_killed", tamper_at="2026-10-13T18:01:00+02:00",
                          tamper_details={"reason": "agent_killed", "at": "2026-10-13T18:01:00+02:00", "detail": "secret-detail"},
                          time_request={"minutes": 30, "at": "2026-10-13T18:02:00+02:00", "expires": "2026-10-13T18:32:00+02:00"},
                          time_request_minutes=30, profile="secret-profile")
    client.ws.push({"type": "state", "pc": pc, "users": users})
    await hass.async_block_till_done()
    assert get("binary_sensor.testpc_service_problem").state == "on" and get("sensor.testpc_service_errors").state == "3"
    assert get("binary_sensor.testpc_kevin_tampering_suspected").state == "on"
    reason = get("sensor.testpc_kevin_tampering_reason")
    assert reason.state == "agent_killed" and reason.attributes["detail"] == "secret-detail" and reason.attributes["options"] == TAMPER_REASONS
    request = get("sensor.testpc_kevin_time_request")
    assert request.state == "30" and request.attributes["expires"] == "2026-10-13T18:32:00+02:00"
    await hass.services.async_call("button", "press", {"entity_id": "button.testpc_kevin_clear_tampering_warning"}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": "button.testpc_kevin_approve_time_request"}, blocking=True)
    assert [c["entity"] for c in client.commands[-2:]] == ["user_kevin_clear_tamper", "user_kevin_approve_time_request"]
    # a reason this integration has never heard of is "unknown", not an invalid state
    users["kevin"]["tamper_reason"] = "something_new"
    client.ws.push({"type": "state", "pc": pc, "users": users})
    await hass.async_block_till_done()
    assert get("sensor.testpc_kevin_tampering_reason").state == "unknown"

    # diagnostics: the states and times stay, the free texts go
    users["kevin"]["tamper_reason"] = "agent_killed"
    client.ws.push({"type": "state", "pc": pc, "users": users})
    await hass.async_block_till_done()
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert "secret" not in json.dumps(data)
    user = data["users"]["user_1"]
    assert user["tamper"] is True and user["tamper_reason"] == "agent_killed" and user["tamper_at"] == "2026-10-13T18:01:00+02:00"
    assert user["tamper_details"] == {"reason": "**REDACTED**", "at": "2026-10-13T18:01:00+02:00", "detail": "**REDACTED**"}
    assert user["time_request"] == {"minutes": 30, "at": "2026-10-13T18:02:00+02:00", "expires": "2026-10-13T18:32:00+02:00"}
    assert user["profile"] == "**REDACTED**"
    assert data["pc"]["degraded"] is True and data["pc"]["tick_failures"] == 3
    assert data["pc"]["health"] == {"degraded": True, "tick_failures": 3, "last_error": "**REDACTED**",
                                    "last_good_tick": "2026-10-13T18:00:05+02:00"}


async def test_new_event_types_from_the_catalog_are_offered_as_triggers(hass: HomeAssistant, fake_client, entry_data) -> None:
    """Event types the PC adds later (time requests, profiles, the weekly summary) need no code here."""
    from homeassistant.components.device_automation import DeviceAutomationType
    from pytest_homeassistant_custom_component.common import async_capture_events, async_get_device_automations

    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    catalog = make_catalog()
    catalog["triggers"] = {"user": {"time_up": "ran out of time", "time_requested": "asked for more time",
                                    "event_of_the_future": "did something new"},
                           "pc": {"started": "service started", "degraded": "service is not working properly"}}
    client.ws.push({"type": "hello", "writable": True, "status": client.health_doc})
    client.ws.push({"type": "entities", **catalog})
    await hass.async_block_till_done()
    device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) if (DOMAIN, "testpc") in d.identifiers)
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, device.id)
    kinds = {(t["type"], t.get("subtype")) for t in triggers if t["domain"] == DOMAIN}
    assert {("time_requested", "kevin"), ("event_of_the_future", "kevin"), ("degraded", None)} <= kinds
    events = async_capture_events(hass, "pclink_event")
    client.ws.push({"type": "event", "ts": 1.0, "user": "kevin", "event": "time_requested", "minutes": 30})
    await hass.async_block_till_done()
    assert events[-1].data["event"] == "time_requested" and events[-1].data["minutes"] == 30 and events[-1].data["user"] == "kevin"


# -- the everyday token and the admin token --------------------------------------------------------
async def test_client_maps_a_403_to_a_clear_refusal_not_to_an_auth_error() -> None:
    from custom_components.pclink.api import PclinkAdminRequiredError, PclinkAuthError, PclinkClient, PclinkCommandError
    from test_fixes import _Response, _Session

    session = _Session({"/api/command": _Response(403, b'{"ok": false, "error": "admin token required (api.admin_token)"}'),
                        "/api/auth": _Response(200, b'{"ok": true, "writable": true, "admin": false, "config_edit": false}'),
                        "/api/config": _Response(403, b'{"error": "admin token required (api.admin_token)"}')})
    client = PclinkClient(session, "192.168.1.50", 47392, "everyday")  # type: ignore[arg-type]
    with pytest.raises(PclinkAdminRequiredError, match="api.admin_token") as err:
        await client.command({"action": "reload"})
    assert isinstance(err.value, PclinkCommandError) and not isinstance(err.value, PclinkAuthError)
    # the everyday token is a valid token for the config flow and for reauth
    assert await client.check_write_auth() == {"writable": True, "admin": False, "config_edit": False}
    # another 403 (something switched off on the PC) carries the PC's own words
    session.answers["/api/command"] = _Response(403, b'{"error": "config editing disabled (api.allow_config_edit)"}')
    with pytest.raises(PclinkCommandError, match="config editing disabled"):
        await client.command({"action": "reload"})
    # a refusal inside a 200 answer (the WebSocket's wording) is recognised as well
    session.answers["/api/command"] = _Response(200, b'{"ok": false, "error": "admin token required (api.admin_token)"}')
    with pytest.raises(PclinkAdminRequiredError):
        await client.command({"action": "reload"})
    # an old service: no /api/auth, and its config probe answering 403 says nothing against the token
    del session.answers["/api/auth"]
    assert await client.check_write_auth() == {"writable": True}


async def test_admin_commands_with_the_everyday_token(hass: HomeAssistant, fake_client, entry_data) -> None:
    from homeassistant import config_entries
    from homeassistant.components.update import UpdateEntityFeature
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.pclink.api import PclinkAdminRequiredError
    from test_update_wake import UPDATE

    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) if (DOMAIN, "testpc") in d.identifiers)
    assert device.configuration_url == "http://192.168.1.50:47392"       # the PC's page asks for the token itself
    catalog = make_catalog()
    catalog["entities"] += [dict(UPDATE), _pc("reload_config", "button", "Reload configuration", category="config", command="reload_config")]
    pc = {**client.pc_state, "pclink_installed_version": "0.3.0", "pclink_latest_version": "0.4.0", "pclink_update_in_progress": False}
    client.ws.push({"type": "entities", **catalog})
    client.ws.push({"type": "state", "pc": pc, "users": client.user_state})
    await hass.async_block_till_done()

    def features() -> UpdateEntityFeature:
        return UpdateEntityFeature(hass.states.get("update.testpc_pc_link_update").attributes["supported_features"])

    assert coordinator.admin is None and UpdateEntityFeature.INSTALL in features()      # an older PC never says: offered as before
    client.ws.push({"type": "hello", "writable": True, "admin": True, "status": client.health_doc})
    await hass.async_block_till_done()
    assert coordinator.admin is True and UpdateEntityFeature.INSTALL in features()
    client.ws.push({"type": "hello", "writable": True, "admin": False, "status": client.health_doc})
    await hass.async_block_till_done()
    assert coordinator.admin is False and UpdateEntityFeature.INSTALL not in features()
    assert hass.states.get("update.testpc_pc_link_update").state == "on"                 # the update is still shown

    async def refused(data):
        raise PclinkAdminRequiredError("admin token required (api.admin_token)")

    with patch.object(client, "command", refused):
        with pytest.raises(HomeAssistantError, match=r"admin token \(api\.admin_token\)"):
            await hass.services.async_call("button", "press", {"entity_id": "button.testpc_reload_configuration"}, blocking=True)
        with pytest.raises(HomeAssistantError, match=r"admin token \(api\.admin_token\)"):
            await hass.services.async_call(DOMAIN, "reload_config", {}, blocking=True)
        with pytest.raises(HomeAssistantError, match=r"admin token \(api\.admin_token\)"):
            await coordinator.live["pclink_update"].async_install(None, False)
    await hass.async_block_till_done()
    # a valid token that may not do one thing is no reason to ask for another
    assert not [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == config_entries.SOURCE_REAUTH]
    assert coordinator.online and hass.states.get("sensor.testpc_cpu_usage").state == "12.5"


async def test_https_entry_links_to_the_https_page(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, {**entry_data, "tls": True, "fingerprint": "ab" * 32})
    device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) if (DOMAIN, "testpc") in d.identifiers)
    assert device.configuration_url == "https://192.168.1.50:47392"
