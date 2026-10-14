"""Fixtures for the Home Assistant integration tests.

Run with a Python environment that has ``pytest-homeassistant-custom-component``
installed (see docs/DEVELOPMENT.md); the regular ``pytest`` run skips this folder.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import aiohttp
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from custom_components.pclink.api import PclinkAuthError, PclinkCommandError, PclinkConnectionError, PclinkOutdatedError  # noqa: E402

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


HEALTH = {"ok": True, "version": "0.3.0", "device": "testpc", "name": "TestPC", "uptime_seconds": 5}


def make_catalog() -> dict[str, Any]:
    pc = [
        {"unique_id": "pclink_testpc_cpu_percent", "key": "cpu_percent", "platform": "sensor", "name": "CPU usage", "value": "cpu_percent",
         "unit": "%", "state_class": "measurement", "icon": "mdi:cpu-64-bit", "enabled": True, "precision": 0, "source": "pc"},
        {"unique_id": "pclink_testpc_last_boot", "key": "last_boot", "platform": "sensor", "name": "Last boot", "value": "last_boot",
         "device_class": "timestamp", "category": "diagnostic", "enabled": True, "source": "pc"},
        {"unique_id": "pclink_testpc_active_sessions", "key": "active_sessions", "platform": "sensor", "name": "Active sessions",
         "value": "active_sessions", "attributes": "sessions", "state_class": "measurement", "enabled": True, "source": "pc"},
        {"unique_id": "pclink_testpc_any_user_active", "key": "any_user_active", "platform": "binary_sensor", "name": "Someone is using the PC",
         "value": "any_user_active", "device_class": "occupancy", "source": "pc"},
        {"unique_id": "pclink_testpc_time_limits_enabled", "key": "time_limits_enabled", "platform": "switch", "name": "Time limits",
         "value": "time_limits_enabled", "icon": "mdi:timer-lock", "category": "config", "command": "time_limits_enabled", "source": "pc"},
        {"unique_id": "pclink_testpc_reboot", "key": "reboot", "platform": "button", "name": "Reboot", "device_class": "restart",
         "command": "reboot", "source": "pc"},
        {"unique_id": "pclink_testpc_broadcast_message", "key": "broadcast_message", "platform": "text", "name": "Message to everyone",
         "icon": "mdi:message-alert", "command": "broadcast_message", "source": "pc"},
        {"unique_id": "pclink_testpc_parent_pin", "key": "parent_pin", "platform": "text", "name": "Parent PIN", "value": "parent_pin_state",
         "password": True, "category": "config", "command": "parent_pin", "source": "pc"},
    ]
    kevin = [
        {"unique_id": "pclink_testpc_user_kevin_status", "key": "user_kevin_status", "platform": "sensor", "name": "kevin status",
         "value": "status", "attributes": "sessions", "device_class": "enum",
         "options": ["logged_off", "active", "idle", "away", "locked", "disconnected"], "icon": "mdi:account", "enabled": True},
        {"unique_id": "pclink_testpc_user_kevin_time_remaining", "key": "user_kevin_time_remaining", "platform": "sensor",
         "name": "kevin time remaining", "value": "remaining_minutes", "attributes": "limit_details", "unit": "min",
         "device_class": "duration", "state_class": "measurement", "enabled": True, "precision": 0},
        {"unique_id": "pclink_testpc_user_kevin_rule_youtube", "key": "user_kevin_rule_youtube", "platform": "sensor",
         "name": "kevin YouTube", "value": "rules.youtube.state", "attributes": "rules.youtube", "device_class": "enum",
         "options": ["ok", "used_up", "blocked", "outside_hours", "off"], "enabled": True},
        {"unique_id": "pclink_testpc_user_kevin_logged_in", "key": "user_kevin_logged_in", "platform": "binary_sensor",
         "name": "kevin logged in", "value": "logged_in", "device_class": "presence"},
        {"unique_id": "pclink_testpc_user_kevin_limit_today", "key": "user_kevin_limit_today", "platform": "number",
         "name": "kevin limit today", "value": "limit_today", "min": 0, "max": 1440, "step": 5, "unit": "min", "mode": "box",
         "category": "config", "command": "user_kevin_limit_today"},
        {"unique_id": "pclink_testpc_user_kevin_blocked", "key": "user_kevin_blocked", "platform": "switch", "name": "kevin access paused",
         "value": "blocked", "icon": "mdi:account-cancel", "command": "user_kevin_blocked"},
        {"unique_id": "pclink_testpc_user_kevin_add_15", "key": "user_kevin_add_15", "platform": "button", "name": "kevin +15 min",
         "icon": "mdi:timer-plus", "command": "user_kevin_add_15"},
        {"unique_id": "pclink_testpc_user_kevin_message", "key": "user_kevin_message", "platform": "text", "name": "kevin message",
         "command": "user_kevin_message"},
        {"unique_id": "pclink_testpc_user_kevin_screenshot_image", "key": "user_kevin_screenshot_image", "platform": "camera",
         "name": "kevin screenshot", "value": "screenshot"},
    ]
    for e in kevin:
        e.update(source="user", user="kevin", user_name="kevin", managed=True)
    return {
        "device": {"identifiers": ["pclink_testpc"], "name": "TestPC", "manufacturer": "home-assistant-pc-link",
                   "model": "Windows PC", "sw_version": "0.3.0"},
        "version": "0.3.0",
        "users": {"kevin": {"name": "kevin", "managed": True}},
        "entities": pc + kevin,
        "triggers": {"user": {"time_up": "ran out of time", "logged_in": "logged in"}, "pc": {"started": "service started"}},
    }


def make_state() -> tuple[dict[str, Any], dict[str, Any]]:
    pc = {"cpu_percent": 12.5, "last_boot": "2026-09-28T06:00:00+00:00", "active_sessions": 1,
          "sessions": [{"id": 2, "user": "kevin", "state": "active"}], "any_user_active": True,
          "time_limits_enabled": True, "parent_pin_state": "", "device_name": "TestPC"}
    users = {"kevin": {"user": "kevin", "name": "kevin", "managed": True, "status": "active", "logged_in": True,
                       "remaining_minutes": 42.4, "limit_today": 90, "blocked": False,
                       "limit_details": {"limit_minutes": 90, "action": "logoff"},
                       "rules": {"youtube": {"state": "ok", "used_minutes": 3, "limit_minutes": 30}},
                       "sessions": [{"id": 2, "state": "active"}], "screenshot": None}}
    return pc, users


class FakeWs:
    """Stand-in for aiohttp's ClientWebSocketResponse: the test pushes server messages."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def push(self, message: dict[str, Any]) -> None:
        self.queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message)))

    async def send_str(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None))

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.queue.get()
        if msg.type == aiohttp.WSMsgType.CLOSED:
            self.closed = True
            raise StopAsyncIteration
        return msg


class FakeClient:
    """Replaces PclinkClient; records commands and lets tests drive the WebSocket."""

    instances: list["FakeClient"] = []

    def __init__(self, session, host, port, token, tls=False, fingerprint=None) -> None:
        self.host, self.port, self.token = host, int(port), token
        self.tls, self.fingerprint = bool(tls or fingerprint), fingerprint
        self.recorded_events: list[dict[str, Any]] = []   # what GET /api/events returns (newest first)
        self.event_requests = 0
        self.health_doc = dict(HEALTH)
        self.catalog = make_catalog()
        self.pc_state, self.user_state = make_state()
        self.commands: list[dict[str, Any]] = []
        self.refuse: Optional[str] = None       # error text to return for every command
        self.fail: Optional[Exception] = None   # raised by every REST call
        self.auth_ok = token == "secret"
        self.write_ok = True                    # False: reads work (loopback) but the token is not valid for writes
        self.auth_probes = 0
        self.ws: Optional[FakeWs] = None
        self.ws_connects = 0
        self.screenshot_bytes = b"JPEGDATA"
        FakeClient.instances.append(self)

    @property
    def base_url(self) -> str:
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}"

    async def events(self, n=50, user=None, since=None, announced=False):
        self._check()   # like an older service: ``since`` and ``announced`` are ignored, the log is returned
        self.event_requests += 1
        return json.loads(json.dumps(self.recorded_events[:n]))

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail
        if not self.auth_ok:
            raise PclinkAuthError("token rejected")

    async def health(self):
        self._check()
        return dict(self.health_doc)

    async def entities(self):
        self._check()
        if self.catalog is None:
            raise PclinkOutdatedError("no catalog")
        return json.loads(json.dumps(self.catalog))

    async def check_write_auth(self):
        self._check()
        self.auth_probes += 1
        if not self.write_ok:
            raise PclinkAuthError("token rejected")
        return {"writable": True}

    async def status(self):
        self._check()
        return json.loads(json.dumps(self.pc_state))

    async def users(self):
        self._check()
        return json.loads(json.dumps(self.user_state))

    async def send(self, data):
        self._check()
        self.commands.append(data)
        if self.refuse:
            return {"ok": False, "error": self.refuse}
        return {"ok": True, "echo": data.get("action")}

    async def command(self, data):
        result = await self.send(data)
        if not result.get("ok"):
            raise PclinkCommandError(result["error"])
        return result

    async def set_entity(self, key, value, pin=None):
        data = {"action": "set", "entity": key, "value": value}
        if pin:
            data["pin"] = pin
        return await self.command(data)

    async def screenshot(self, user):
        self._check()
        return self.screenshot_bytes if user == "kevin" else None

    async def ws_connect(self):
        self._check()
        self.ws_connects += 1
        self.ws = FakeWs()
        return self.ws

    @staticmethod
    async def ws_request(ws, kind, req_id, **fields):
        await ws.send_str(json.dumps({"type": kind, "id": req_id, **fields}))


@pytest.fixture
def fake_client():
    FakeClient.instances.clear()
    with patch("custom_components.pclink.PclinkClient", FakeClient), \
            patch("custom_components.pclink.config_flow.PclinkClient", FakeClient):
        yield FakeClient


@pytest.fixture
def entry_data():
    return {"host": "192.168.1.50", "port": 47392, "token": "secret"}
