"""ASTER multiseat: seen from the service, every workplace after the first is a session listed
as *disconnected* and named "Multiseat".  It is a seat in use all the same."""
from __future__ import annotations

import logging
import time

from pclink.winsessions import WindowsSessions, aster_seat

from .test_fix_core import NOW, build, report, tick, user_state

WTS_ACTIVE, WTS_DISCONNECTED = 0, 4


class _AsterWts:
    """WTSEnumerateSessions as ASTER answers it in session 0."""
    WTSUserName, WTSDomainName = 5, 7

    def __init__(self, entries):
        self.entries = entries

    def WTSEnumerateSessions(self, _server):
        return self.entries

    def WTSQuerySessionInformation(self, _server, sid, what):
        raise AssertionError("WTSINFOEX answered; not asked")


def _aster_sessions(extra=()):
    entries = [{"SessionId": 0, "State": WTS_DISCONNECTED, "WinStationName": "Services"},
               {"SessionId": 1, "State": WTS_ACTIVE, "WinStationName": "Console"},
               {"SessionId": 2, "State": WTS_DISCONNECTED, "WinStationName": "Multiseat"},
               *extra]
    users = {1: "dad", 2: "kevin", 3: "mum"}
    ws = WindowsSessions.__new__(WindowsSessions)
    ws._wts = _AsterWts(entries)
    # Windows says the ASTER workplace is locked: its lock flag is not that of the seat
    ws._query_ex = lambda sid: {"user": users[sid], "domain": "PC", "station": "", "state": 0,
                                "locked": sid == 2 or None, "idle_seconds": 3.0, "logon_time": None}
    ws.lock_flag_inverted = False
    ws._known, ws._failed = {}, set()
    return ws


def test_an_aster_workplace_is_an_active_seat(caplog):
    assert aster_seat("disconnected", "Multiseat") and aster_seat("disconnected", " MULTISEAT ")
    assert not aster_seat("disconnected", "") and not aster_seat("active", "Multiseat")
    assert not aster_seat("disconnected", "RDP-Tcp#3")
    ws = _aster_sessions(extra=[{"SessionId": 3, "State": WTS_DISCONNECTED, "WinStationName": ""}])
    with caplog.at_level(logging.INFO, logger="pclink.winsessions"):
        got = {s.session_id: s for s in ws.list_sessions()}
        ws.list_sessions()
    assert (got[1].state, got[1].station) == ("active", "Console")
    assert (got[2].state, got[2].station, got[2].locked) == ("active", "Multiseat", None)   # left to its agent
    assert got[3].state == "disconnected"   # a session left by switching user is no seat in use
    assert caplog.text.count("ASTER workplace") == 1


def test_both_aster_seats_get_an_agent_activity_and_counted_time():
    orch, _fake, api, agents = build(launcher=True)
    orch.sessions = _aster_sessions()
    agents.connected.update({1, 2})
    tick(orch)
    assert sorted(h.session_id for h in orch.launcher.launched) == [1, 2]
    for _ in range(12):
        report(orch, sid=1, name="excel.exe")
        report(orch, sid=2, name="minecraft.exe")
        tick(orch, 5)
    kevin, dad = user_state(api, "kevin"), user_state(api, "dad")
    assert kevin["status"] == "active" and kevin["presence"] == "active"
    assert kevin["app"] == "minecraft.exe" and dad["app"] == "excel.exe"
    assert orch.store.usage("kevin", NOW.date()).active >= 55   # the second seat's time counts toward its limit
    assert kevin["remaining_minutes"] < 60
    # the agents hear that another seat is in use: neither counts a machine-wide signal as its own
    status = [(sid, m["other_seats"]) for sid, m in agents.sent if m["type"] == "status"]
    assert {sid for sid, _n in status} == {1, 2} and all(n == 1 for _sid, n in status)


def test_a_locked_aster_seat_is_known_from_its_agent():
    orch, _fake, api, agents = build(launcher=True)
    orch.sessions = _aster_sessions()
    agents.connected.update({1, 2})
    tick(orch)
    report(orch, sid=2, name=None, locked=True)
    tick(orch, 5)
    assert user_state(api, "kevin")["status"] == "locked"


def test_the_agent_takes_the_service_count_of_other_seats():
    from pclink.agent import win_presence as p
    from pclink.agent.main import Agent

    signals = p.PresenceSignals(controller=False)
    signals._other_seats, signals._seats_checked = False, time.monotonic()   # Windows shows this seat alone
    agent = Agent.__new__(Agent)
    agent.presence, agent.tray = signals, None
    agent._status_text = ""
    agent._apply_status({"type": "status", "text": "x", "other_seats": 1})
    assert signals.service_other_seats == 1
    assert signals.sample(5.0)["display_required"] is False and signals.controller.counts is False
    agent._apply_status({"type": "status", "text": "x", "other_seats": 0})
    signals.sample(5.0)
    assert signals.controller.counts is True
    agent._apply_status({"type": "status", "text": "x", "other_seats": True})   # not a count: ignored
    assert signals.service_other_seats == 0
    agent._apply_status({"type": "status", "text": "x"})                         # an older service says nothing
    assert signals.service_other_seats == 0
