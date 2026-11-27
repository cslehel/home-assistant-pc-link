"""Hydra Multi-Seat, and seats (ASTER or Hydra) that cannot be locked.

Seen from the service, every Hydra seat after the console is a session listed as
*disconnected* without a telling station name; while Hydra is on, such a session is a
seat in use.  A seat that Windows will not lock is not disconnected (no effect on ASTER,
Hydra repairs the seat): when the lock enforces a limit, the seat is logged off instead."""
from __future__ import annotations

import logging

import pytest

from pclink.config import Config, ConfigError
from pclink.models import SessionInfo
from pclink.winsessions import HydraDetector, WindowsSessions, multiseat_seat

from .test_fix_core import BASE, build, events, tick, user_state

WTS_ACTIVE, WTS_DISCONNECTED = 0, 4
LOCK_CFG = BASE.replace("grace_seconds: 0", "grace_seconds: 0\n      action: lock")


# -- which sessions are seats --------------------------------------------------------------------------

def test_multiseat_seat_by_software_and_mode():
    assert multiseat_seat("disconnected", "Multiseat") == "aster"
    assert multiseat_seat("disconnected", "", hydra=True) == "hydra"
    assert multiseat_seat("disconnected", "Multiseat", hydra=True) == "aster"
    assert multiseat_seat("disconnected", "") is None                          # Hydra off: switch user
    assert multiseat_seat("disconnected", "RDP-Tcp#2", hydra=True) is None     # remote desktop is no seat
    assert multiseat_seat("active", "Console", hydra=True) is None             # active anyway
    assert multiseat_seat("disconnected", "", mode="hydra") == "hydra"         # told, not detected
    assert multiseat_seat("disconnected", "Multiseat", mode="hydra") == "hydra"
    assert multiseat_seat("disconnected", "", mode="aster", hydra=True) is None
    assert multiseat_seat("disconnected", "Multiseat", mode="off") is None
    assert multiseat_seat("disconnected", "", mode="off", hydra=True) is None


class _Wts:
    WTSUserName, WTSDomainName = 5, 7

    def __init__(self, entries):
        self.entries = entries

    def WTSEnumerateSessions(self, _server):  # noqa: N802
        return self.entries

    def WTSQuerySessionInformation(self, _server, sid, what):  # noqa: N802
        raise AssertionError("WTSINFOEX answered; not asked")


def _hydra_sessions(mode="auto", hydra_on=True, extra=()):
    entries = [{"SessionId": 0, "State": WTS_DISCONNECTED, "WinStationName": "Services"},
               {"SessionId": 1, "State": WTS_ACTIVE, "WinStationName": "Console"},
               {"SessionId": 2, "State": WTS_DISCONNECTED, "WinStationName": ""},
               *extra]
    users = {1: "dad", 2: "kevin", 3: "mum", 4: "sarah"}
    ws = WindowsSessions.__new__(WindowsSessions)
    ws._wts = _Wts(entries)
    ws._query_ex = lambda sid: {"user": users[sid], "domain": "PC", "station": "", "state": 0,
                                "locked": sid != 1 or None, "idle_seconds": 3.0, "logon_time": None}
    ws.lock_flag_inverted = False
    ws._known, ws._failed = {}, set()
    ws.multiseat_mode = mode
    ws.hydra = lambda: hydra_on
    return ws


def test_a_hydra_seat_is_an_active_seat_left_to_its_agent(caplog):
    ws = _hydra_sessions(extra=[{"SessionId": 3, "State": WTS_DISCONNECTED, "WinStationName": "RDP-Tcp#5"}])
    with caplog.at_level(logging.INFO, logger="pclink.winsessions"):
        got = {s.session_id: s for s in ws.list_sessions()}
        ws.list_sessions()
    assert (got[1].state, got[1].seat) == ("active", None)
    assert (got[2].state, got[2].seat, got[2].locked) == ("active", "hydra", None)
    assert got[3].state == "disconnected" and got[3].seat is None
    assert caplog.text.count("Hydra Multi-Seat workplace") == 1
    assert ws.multiseat() == "hydra"
    assert got[2].to_dict()["seat"] == "hydra"


def test_without_hydra_a_disconnected_session_stays_one():
    ws = _hydra_sessions(hydra_on=False)
    got = {s.session_id: s for s in ws.list_sessions()}
    assert got[2].state == "disconnected" and got[2].seat is None and got[2].locked is True
    assert ws.multiseat() is None


def test_mode_off_maps_no_seat_and_mode_hydra_needs_no_detection():
    aster = [{"SessionId": 3, "State": WTS_DISCONNECTED, "WinStationName": "Multiseat"}]
    off = {s.session_id: s for s in _hydra_sessions(mode="off", extra=aster).list_sessions()}
    assert off[2].state == off[3].state == "disconnected"
    told = _hydra_sessions(mode="hydra", hydra_on=False)
    assert {s.session_id: s.seat for s in told.list_sessions()}[2] == "hydra"
    aster_only = _hydra_sessions(mode="auto", hydra_on=False, extra=aster)
    assert {s.session_id: s.seat for s in aster_only.list_sessions()}[3] == "aster"
    assert aster_only.multiseat() == "aster"


def test_hydra_detector_flag_file_service_and_cache(tmp_path):
    flag = tmp_path / "hydra-armed.flag"
    now = [0.0]
    running = [False]
    calls = []

    def service():
        calls.append(1)
        return running[0]

    det = HydraDetector(str(flag), service_running=service, clock=lambda: now[0], interval=60)
    assert det() is False
    flag.write_text("")
    assert det() is False            # asked again only after a minute
    now[0] = 61
    assert det() is True
    flag.unlink()
    running[0] = True
    now[0] = 130
    assert det() is True             # the running service is enough
    det2 = HydraDetector(str(flag), service_running=lambda: 1 / 0, clock=lambda: 0.0)
    assert det2() is False           # a failing check is "not on", never a crash
    assert len(calls) == 2


# -- configuration ------------------------------------------------------------------------------------------

def test_multiseat_config_is_validated():
    cfg = Config.from_dict({"multiseat": {"mode": "Hydra", "lock_fallback": "NONE"}})
    assert (cfg.multiseat.mode, cfg.multiseat.lock_fallback) == ("hydra", "none")
    assert (Config.from_dict({}).multiseat.mode, Config.from_dict({}).multiseat.lock_fallback) == ("auto", "logoff")
    with pytest.raises(ConfigError, match="multiseat.mode"):
        Config.from_dict({"multiseat": {"mode": "aster-ish"}})
    with pytest.raises(ConfigError, match="multiseat.lock_fallback"):
        Config.from_dict({"multiseat": {"lock_fallback": "shutdown"}})


# -- a seat that cannot be locked ---------------------------------------------------------------------------

def _seat(sid=2, seat="aster", locked=None):
    return SessionInfo(session_id=sid, user="kevin", station="Multiseat" if seat == "aster" else "", state="active",
                       locked=locked, idle_seconds=0.0, seat=seat)


def _out_of_time(text=LOCK_CFG, seat="aster"):
    orch, sessions, api, agents = build(text)
    orch.timekeeper.set_limit_today("kevin", orch._clocks.now.date(), 0)
    sessions.sessions = [_seat(seat=seat)]
    agents.connected.add(2)
    return orch, sessions, api, agents


def test_a_seat_the_agent_could_not_lock_is_logged_off_at_once():
    orch, sessions, api, agents = _out_of_time()
    tick(orch)
    tick(orch, 1)
    assert len(agents.of("lock")) == 1 and sessions.logged_off == []
    orch.on_agent_message(2, {"type": "lock_result", "ok": False})
    tick(orch, 1)
    assert sessions.logged_off == [2] and sessions.disconnected == []
    assert any("could not be locked" in text for _sid, _title, text in sessions.messages) \
        or any(m.get("message", "").startswith("This seat could not be locked") for m in agents.of("notify"))
    assert events(api, "lock_failed") == [("user/kevin", "lock_failed")]
    assert "fallback=logoff" in orch.store.recent_events(5, kind="lock_failed")[0]["detail"]


def test_a_seat_still_unlocked_at_the_next_try_is_logged_off_not_disconnected():
    orch, sessions, _api, agents = _out_of_time(seat="hydra")
    for _ in range(8):   # the agent never answers (an older one) and the seat stays unlocked
        tick(orch, 5)
    assert len(agents.of("lock")) == 1
    assert sessions.logged_off == [2] and sessions.disconnected == []


def test_a_locked_seat_is_left_alone():
    orch, sessions, api, agents = _out_of_time()
    tick(orch)
    tick(orch, 1)   # the grace warning, then the lock
    sessions.sessions = [_seat(locked=True)]
    for _ in range(10):
        tick(orch, 30)
    orch.on_agent_message(2, {"type": "lock_result", "ok": False})   # a late answer for a seat that is locked
    tick(orch, 1)
    assert sessions.logged_off == [] and events(api, "lock_failed") == []
    assert len(agents.of("lock")) == 1   # not asked again while it stays locked


def test_lock_fallback_none_reports_once_and_keeps_asking():
    orch, sessions, api, agents = _out_of_time(LOCK_CFG + "multiseat: {lock_fallback: none}\n")
    for _ in range(40):   # 200 s, the seat never locks
        tick(orch, 5)
    assert sessions.logged_off == [] and sessions.disconnected == []
    assert events(api, "lock_failed") == [("user/kevin", "lock_failed")]
    assert 3 <= len(agents.of("lock")) <= 5   # about every LOCK_RETRY_SECONDS (+ the confirm time)


def test_a_lock_from_home_assistant_never_logs_a_seat_off():
    orch, sessions, api, agents = build()
    orch.timekeeper.set_limit_today("kevin", orch._clocks.now.date(), 600)
    sessions.sessions = [_seat()]
    agents.connected.add(2)
    tick(orch)
    orch.api_command({"action": "lock", "user": "kevin"})
    orch.on_agent_message(2, {"type": "lock_result", "ok": False})
    tick(orch, 1)
    assert sessions.logged_off == [] and sessions.disconnected == []
    assert events(api, "lock_failed") == [("user/kevin", "lock_failed")]


def test_an_ordinary_session_the_agent_could_not_lock_is_disconnected():
    orch, sessions, _api, agents = build(LOCK_CFG)
    orch.timekeeper.set_limit_today("kevin", orch._clocks.now.date(), 0)
    sessions.sessions = [SessionInfo(session_id=2, user="kevin", state="active", locked=False, idle_seconds=0.0)]
    agents.connected.add(2)
    tick(orch)
    tick(orch, 1)
    orch.on_agent_message(2, {"type": "lock_result", "ok": False})
    tick(orch, 1)
    assert sessions.disconnected == [2] and sessions.logged_off == []


def test_an_unasked_lock_result_is_dropped():
    orch, sessions, _api, _agents = build()
    sessions.sessions = [_seat()]
    tick(orch)
    orch.on_agent_message(2, {"type": "lock_result", "ok": False})
    assert orch._commands.empty()


def test_the_agent_answers_a_lock_request(monkeypatch):
    from pclink.agent import main as agent_main

    sent = []

    class Client:
        def send(self, msg):
            sent.append(msg)
            return True

    agent = agent_main.Agent.__new__(agent_main.Agent)
    agent.client = Client()
    for ok in (False, True):
        monkeypatch.setattr(agent_main, "lock_workstation", lambda ok=ok: ok)
        agent._handle({"type": "lock"})
    assert sent == [{"type": "lock_result", "ok": False}, {"type": "lock_result", "ok": True}]


def test_the_service_info_names_the_multiseat_software():
    orch, sessions, api, _agents = build()
    sessions.sessions = [_seat(seat="hydra")]
    tick(orch)
    assert api.pc["service_info"]["multiseat"] == "hydra"
    assert user_state(api)["sessions"][0]["seat"] == "hydra"
