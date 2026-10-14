"""Review 3: a command refused with ``retry_after`` (or HTTP 429) - the integration waits as
the PC asked: nothing is sent or polled before then, and the error says so, translated."""
from __future__ import annotations

import json
import os
import time

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pclink.api import PclinkClient, PclinkCommandError, PclinkThrottledError, reply_retry_after
from custom_components.pclink.const import DOMAIN

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def setup_entry(hass: HomeAssistant, entry_data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def throttling(client, seconds=30):
    """Make the fake PC refuse every command with a retry_after, the way the real client reports it."""
    async def command(data):
        client.commands.append(data)
        raise PclinkThrottledError(f"too many wrong tokens; try again in {seconds} s", float(seconds))
    client.command = command


def test_reply_retry_after() -> None:
    assert reply_retry_after({"ok": False, "error": "x", "retry_after": 30}) == 30
    assert reply_retry_after({"ok": False, "retry_after": 0}) == 1
    assert reply_retry_after({"ok": False, "retry_after": 10 ** 9}) == 3600
    for reply in ({"ok": True, "retry_after": 5}, {"ok": False}, {"ok": False, "retry_after": "5"},
                  {"ok": False, "retry_after": True}, {"ok": False, "retry_after": float("nan")}, None, []):
        assert reply_retry_after(reply) is None


async def test_client_raises_throttled_for_a_refusal_with_retry_after(hass: HomeAssistant, aioclient_mock) -> None:
    client = PclinkClient(async_get_clientsession(hass), "192.168.1.50", 47392, "secret")
    aioclient_mock.post("http://192.168.1.50:47392/api/command",
                        json={"ok": False, "error": "too many wrong tokens; try again in 20 s", "retry_after": 20})
    with pytest.raises(PclinkThrottledError) as caught:
        await client.command({"action": "lock"})
    assert caught.value.retry_after == 20 and "try again in 20 s" in str(caught.value)
    # send() still returns the answer as it is
    assert (await client.send({"action": "lock"}))["retry_after"] == 20
    aioclient_mock.clear_requests()
    aioclient_mock.post("http://192.168.1.50:47392/api/command", json={"ok": False, "error": "parent pin required"})
    with pytest.raises(PclinkCommandError) as caught:
        await client.command({"action": "lock"})
    assert not isinstance(caught.value, PclinkThrottledError)


async def test_rest_429_on_a_command_is_throttled(hass: HomeAssistant, aioclient_mock) -> None:
    client = PclinkClient(async_get_clientsession(hass), "192.168.1.50", 47392, "secret")
    aioclient_mock.post("http://192.168.1.50:47392/api/command", status=429, headers={"Retry-After": "17"},
                        json={"error": "too many wrong tokens; try again in 17 s"})
    with pytest.raises(PclinkThrottledError) as caught:
        await client.command({"action": "lock"})
    assert caught.value.retry_after == 17


async def test_service_raises_a_translated_wait_and_holds_back_commands(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    throttling(client, 30)
    with pytest.raises(HomeAssistantError) as caught:
        await hass.services.async_call(DOMAIN, "lock", {}, blocking=True)
    assert caught.value.translation_key == "throttled" and caught.value.translation_domain == DOMAIN
    assert caught.value.translation_placeholders == {"pc": "TestPC", "seconds": "30"}
    assert len(client.commands) == 1
    assert 25 < coordinator.throttled_for() <= 30
    # the next command is not sent at all while the PC's time is not up
    with pytest.raises(HomeAssistantError) as caught:
        await hass.services.async_call(DOMAIN, "add_minutes", {"user": "kevin", "minutes": 10}, blocking=True)
    assert caught.value.translation_key == "throttled"
    assert int(caught.value.translation_placeholders["seconds"]) <= 30
    assert len(client.commands) == 1
    # with a response: the wait is part of it
    with pytest.raises(HomeAssistantError):   # the only PC failed: raised all the same
        await hass.services.async_call(DOMAIN, "usage_report", {"user": "kevin"}, blocking=True, return_response=True)
    # once the time is up, commands go out again
    coordinator._hold_until = time.monotonic() - 1
    client.command = type(client).command.__get__(client)
    await hass.services.async_call(DOMAIN, "lock", {}, blocking=True)
    assert client.commands[-1]["action"] == "lock" and len(client.commands) == 2


async def test_entity_command_raises_the_translated_wait(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    throttling(client, 45)
    with pytest.raises(HomeAssistantError) as caught:
        await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.testpc_time_limits"}, blocking=True)
    assert caught.value.translation_key == "throttled"
    assert caught.value.translation_placeholders["seconds"] == "45"
    with pytest.raises(HomeAssistantError) as caught:      # held back: not sent
        await hass.services.async_call("button", "press", {"entity_id": "button.testpc_reboot"}, blocking=True)
    assert caught.value.translation_key == "throttled"
    assert len(client.commands) == 1
    assert hass.data[DOMAIN][entry.entry_id].throttled_for() > 0


async def test_a_refusal_with_retry_after_from_send_also_holds(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async def send(data):
        client.commands.append(data)
        return {"ok": False, "error": "try again in 60 s", "retry_after": 60}
    client.send = send
    assert (await coordinator.async_send({"action": "peer_usage", "peers": {}}))["retry_after"] == 60
    with pytest.raises(PclinkThrottledError):
        await coordinator.async_send({"action": "peer_usage", "peers": {}})
    assert len(client.commands) == 1


async def test_no_poll_while_the_pc_asked_to_wait(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.ws_connected = False
    calls = []
    original = client.health

    async def health():
        calls.append(1)
        return await original()
    client.health = health
    coordinator.note_throttle(30)
    with pytest.raises(UpdateFailed, match="asked to wait"):
        await coordinator._async_update_data()
    assert calls == []
    coordinator._hold_until = 0.0
    await coordinator._async_update_data()
    assert calls == [1]
    # a 429 on the poll starts the hold
    async def throttled():
        raise PclinkThrottledError("too many wrong tokens", 40)
    client.health = throttled
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert 35 < coordinator.throttled_for() <= 40


async def test_a_throttled_socket_answer_holds_commands(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = await setup_entry(hass, entry_data)
    client = fake_client.instances[-1]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client.ws.push({"type": "hello", "writable": True, "status": client.health_doc})
    client.ws.push({"type": "result", "id": 12, "ok": False, "error": "too many wrong tokens; try again in 25 s",
                    "retry_after": 25})
    await hass.async_block_till_done()
    assert coordinator.throttled_for() > 20
    with pytest.raises(PclinkThrottledError):
        await coordinator.async_command({"action": "lock"})
    assert client.commands == []


def test_the_throttled_message_is_in_the_strings() -> None:
    for name in ("strings.json", os.path.join("translations", "en.json")):
        with open(os.path.join(ROOT, "custom_components", "pclink", name), encoding="utf-8") as fh:
            message = json.load(fh)["exceptions"]["throttled"]["message"]
        assert "{pc}" in message and "{seconds}" in message
