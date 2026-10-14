"""Agent side of "ask for more time", and the replies to close requests."""
import os
import threading
import types

import pytest

from pclink.agent import tray as traymod
from pclink.agent import win_activity as w


def service_reads_as_failure(result: str) -> bool:
    """The service's rule for a close reply (core.py, the handler of "closed")."""
    return result in ("tab_failed", "noop", "refused", "terminated 0") or result.startswith(("refused", "failed"))


# -- ask for more time ----------------------------------------------------------------------------

def test_offered_amounts_are_cleaned_up():
    assert traymod.time_request_options([15, 30, 60]) == [15, 30, 60]
    assert traymod.time_request_options(None) == [] and traymod.time_request_options([]) == []
    assert traymod.time_request_options("15") == [] and traymod.time_request_options({"a": 1}) == []
    assert traymod.time_request_options([15, 15, 0, -5, 241, 240, "30", True, 2.5, 45.0, None]) == [15, 240, 45]
    assert traymod.time_request_options(range(1, 20)) == []                       # not a list: not from the service
    assert traymod.time_request_options(list(range(1, 20))) == [1, 2, 3, 4, 5, 6]  # six entries at most
    assert traymod.time_request_options([float("nan"), float("inf"), 10]) == [10]


def test_menu_entries_and_what_a_click_means():
    options = [15, 30, 60, 90, 120, 1]
    items = traymod.request_menu_items(options)
    assert [label for _id, label in items] == ["15 minutes", "30 minutes", "1 hour", "90 minutes", "2 hours", "1 minute"]
    ids = [ident for ident, _label in items]
    assert len(set(ids)) == 6 and not {1023, 1024, 1025} & set(ids)               # never one of the fixed entries
    assert [traymod.request_minutes(i, options) for i in ids] == options
    assert traymod.request_minutes(ids[-1] + 1, options) is None and traymod.request_minutes(1023, options) is None
    assert traymod.request_menu_items([]) == []                                   # nothing offered: no entry at all


def test_a_second_click_within_seconds_is_swallowed():
    guard = traymod.RequestGuard(gap=5.0)
    assert guard.allow(100.0)
    assert not guard.allow(100.2) and not guard.allow(104.9)
    assert guard.allow(105.0)
    assert not guard.allow(106.0)
    guard.undo()                                                                  # that one could not be sent
    assert guard.allow(106.5)


class FakeGui:
    MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_POPUP, TPM_LEFTALIGN, WM_NULL = 0, 1, 0x800, 0x10, 0, 0

    def __init__(self):
        self.menus = {}

    def CreatePopupMenu(self):
        handle = 100 + len(self.menus)
        self.menus[handle] = []
        return handle

    def AppendMenu(self, menu, flags, ident, text):
        self.menus[menu].append((flags, ident, text))

    def GetCursorPos(self):
        return (0, 0)

    def SetForegroundWindow(self, hwnd):
        pass

    def TrackPopupMenu(self, *args):
        pass

    def PostMessage(self, *args):
        pass

    def DestroyMenu(self, menu):
        pass


def _tray(monkeypatch, on_request):
    import sys

    gui = FakeGui()
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    tray = traymod.TrayIcon(on_time_request=on_request)
    tray._win32con, tray._hwnd = gui, 1
    tray.ID_OPEN, tray.ID_STATUS, tray.ID_QUIT = 1023, 1024, 1025
    return tray, gui


def test_menu_shows_the_entry_only_when_amounts_are_offered(monkeypatch):
    asked = []
    tray, gui = _tray(monkeypatch, asked.append)
    tray._popup_menu()
    texts = [text for _f, _i, text in gui.menus[100]]
    assert "Ask for more time" not in texts and len(gui.menus) == 1               # hidden: nothing is offered

    tray.set_time_requests([15, 60])
    tray._popup_menu()
    main_menu, sub = gui.menus[101], gui.menus[102]
    assert main_menu[-1][2] == "Ask for more time" and main_menu[-1][0] == gui.MF_POPUP and main_menu[-1][1] == 102
    assert [text for _f, _i, text in sub] == ["15 minutes", "1 hour"]
    tray._on_command(1, 0, sub[1][1], 0)
    assert asked == [60]
    tray.set_time_requests([30])                                                  # the offer changes while a menu is open:
    tray._on_command(1, 0, sub[0][1], 0)                                          # the click means what the menu showed
    assert asked == [60, 15]
    tray._on_command(1, 0, sub[1][1] + 5, 0)                                      # not an entry
    assert asked == [60, 15]

    tray.set_time_requests([])
    tray._popup_menu()
    assert "Ask for more time" not in [text for _f, _i, text in gui.menus[103]]

    quiet, gui2 = _tray(monkeypatch, None)                                        # no handler (not the agent's tray)
    quiet.set_time_requests([15])
    quiet._popup_menu()
    assert "Ask for more time" not in [text for _f, _i, text in gui2.menus[100]]


def _agent(connected=True):
    """An Agent without its Windows parts (the same idea as in test_fix_agent.py)."""
    from pclink.agent.main import Agent

    agent = Agent.__new__(Agent)
    agent.sent, agent.tray_states, agent.offers, agent.balloons = [], [], [], []
    agent.client = types.SimpleNamespace(connected=threading.Event(), send=None)
    agent.tray = types.SimpleNamespace(update=lambda state, tooltip=None, detail=None: agent.tray_states.append((state, detail)),
                                       can_notify=True, notify=lambda *a: agent.balloons.append(a) or True,
                                       set_time_requests=agent.offers.append)
    agent.presence = types.SimpleNamespace(last_quns=None)
    agent.activity = types.SimpleNamespace(last_created=0.0)
    agent._tray_connected, agent._last_status, agent._status_text = False, None, "Connecting..."
    agent._boxes_open, agent._boxes_lock = 0, threading.Lock()
    agent._request_guard = traymod.RequestGuard()
    agent.send_ok = connected
    agent.client.send = lambda m: (agent.sent.append(m) or True) if agent.send_ok else False
    return agent


def test_status_drives_the_menu_and_a_click_sends_one_request(monkeypatch):
    from pclink.agent import main

    clock = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    agent = _agent()
    agent._on_message({"type": "status", "text": "10 min left today", "remaining_minutes": 10, "time_requests": [15, 30]})
    assert agent.offers[-1] == [15, 30]
    agent._request_time(30)
    assert agent.sent == [{"type": "time_request", "minutes": 30}]
    clock[0] += 1.0
    agent._request_time(30)                                                       # the double click
    agent._request_time(15)
    assert len(agent.sent) == 1
    clock[0] += 10.0
    agent._request_time(15)
    assert agent.sent[-1] == {"type": "time_request", "minutes": 15} and len(agent.sent) == 2
    clock[0] += 10.0
    agent._request_time(45)                                                       # never offered: never sent
    assert len(agent.sent) == 2

    agent._on_message({"type": "status", "text": "No time limit"})                # no "time_requests": not offered
    assert agent.offers[-1] is None
    agent._request_time(15)
    assert len(agent.sent) == 2


def test_request_that_cannot_be_sent_says_so_and_may_be_repeated(monkeypatch):
    from pclink.agent import main

    clock = [50.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    agent = _agent(connected=False)
    agent._on_message({"type": "status", "text": "x", "time_requests": [60]})
    agent._request_time(60)
    assert agent.sent == [] and "1 hour" in agent.balloons[-1][1] and "not connected" in agent.balloons[-1][1]
    agent.send_ok = True
    clock[0] += 0.5
    agent._request_time(60)                                                       # at once: the first never left
    assert agent.sent == [{"type": "time_request", "minutes": 60}]


def test_menu_entry_goes_away_while_the_service_is_gone():
    agent = _agent()
    agent.client.connected.set()
    agent._on_message({"type": "status", "text": "x", "time_requests": [15]})
    agent._update_tray_connection()
    agent.client.connected.clear()
    agent._update_tray_connection()
    assert agent.offers[-1] == []
    agent.client.connected.set()
    agent._update_tray_connection()                                               # back: the last status applies again
    assert agent.offers[-1] == [15]


# -- replies to close requests ---------------------------------------------------------------------

def test_close_reply_strings_fit_the_services_convention():
    for count in (1, 2, 10, 100):
        assert not service_reads_as_failure(w.close_reply(count))                 # "terminated 10" is not "terminated 0"
        assert not service_reads_as_failure(w.close_reply(count, w.WHY_DENIED))
    assert w.close_reply(0) == "terminated 0"
    for why in ("", w.WHY_PROTECTED, w.WHY_REUSED, w.WHY_DENIED, w.WHY_NOOP):
        result = w.close_reply(0, why)
        assert service_reads_as_failure(result) and len(result) <= 40, result
    assert service_reads_as_failure("tab_failed") and not service_reads_as_failure("tab_closed")


@pytest.mark.skipif(os.name != "nt", reason="terminating processes is only done on Windows")
def test_every_refusal_path_tells_the_service_why():
    import subprocess
    import sys

    import psutil

    assert w.close_app(os.getpid(), "") == w.WHY_PROTECTED                        # the agent itself
    assert w.close_app(os.getppid(), "") == w.WHY_PROTECTED
    assert w.close_app(0, "explorer.exe") == w.WHY_PROTECTED
    assert w.close_app(0, "") == w.WHY_NOOP
    assert w.close_app(0, "no-such-program-zz9.exe") == "terminated 0"
    assert w.close_app(2 ** 22 + 3, "whatever.exe") == "terminated 0"             # the process is gone
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        proc = psutil.Process(child.pid)
        name, created = proc.name(), proc.create_time()
        assert w.close_app(child.pid, "another-name.exe", created) == w.WHY_REUSED
        assert w.close_app(child.pid, name, created + 30) == w.WHY_REUSED        # creation time differs: a reused pid
        assert child.poll() is None                                               # ... and nothing was touched
        result = w.close_app(child.pid, name, created)
        assert result == "terminated 1" and not service_reads_as_failure(result)
        child.wait(10)
    finally:
        if child.poll() is None:
            child.kill()


def test_agent_replies_to_every_close_request(monkeypatch):
    from pclink.agent import main

    agent = _agent()
    outcomes = iter(["terminated 1", w.WHY_REUSED, "terminated 0"])
    seen = []
    monkeypatch.setattr(main, "close_app", lambda pid, name, created: seen.append((pid, name, created)) or next(outcomes))
    monkeypatch.setattr(main, "close_tab", lambda pid, title: title == "still there")
    agent._close({"type": "close", "pid": 7, "name": "game.exe", "created": 1700000000.5, "reason": "limit"})
    agent._close({"type": "close", "pid": 7, "name": "game.exe", "created": "nonsense"})
    agent._close({"type": "close", "name": "game.exe"})
    agent._close({"type": "close", "pid": 9, "name": "chrome.exe", "tab": True, "title": "still there"})
    agent._close({"type": "close", "pid": 9, "name": "chrome.exe", "tab": True, "title": "moved on"})   # also: elevated browser
    results = [m["result"] for m in agent.sent]
    assert results == ["terminated 1", w.WHY_REUSED, "terminated 0", "tab_closed", "tab_failed"]
    assert [service_reads_as_failure(r) for r in results] == [False, True, True, False, True]
    assert seen == [(7, "game.exe", 1700000000.5), (7, "game.exe", 0.0), (0, "game.exe", 0.0)]
    assert all(m["type"] == "closed" for m in agent.sent) and agent.sent[0]["reason"] == "limit"


def test_service_settings_are_used(monkeypatch):
    from pclink.agent.main import Watchdog

    monkeypatch.setenv("PCLINK_SERVICE_PID", "31337")
    assert 31337 in w.protected_pids()
    assert w.close_process(31337, "") == 0                                        # whatever the request names
    why = []
    w.close_process(31337, "anything.exe", 0.0, why)
    assert why in ([w.WHY_PROTECTED], [w.WHY_NOOP])                               # (noop off Windows)
    # poll_watchdog_seconds from the agent settings: the limit, never under a few polls, 0 = off
    settings = {"poll_watchdog_seconds": 45, "poll_seconds": 2.0}
    limit = float(settings["poll_watchdog_seconds"])
    assert Watchdog(max(limit, settings["poll_seconds"] * 5)).limit == 45.0
    assert not Watchdog(0.0).expired(1e9)
