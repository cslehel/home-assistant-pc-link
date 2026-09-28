"""LogonBlocker with win32net and net.exe replaced by fakes (runs on any OS)."""
from __future__ import annotations

import subprocess

import pytest

from pclink import logonblock
from pclink.logonblock import UF_ACCOUNTDISABLE, LogonBlocker, NullBlocker, make_blocker


class FakeNet:
    def __init__(self, hours: bytes = bytes([0xFF] * 21), flags: int = 0x201):
        self.hours = hours
        self.flags = flags
        self.set_calls: list[tuple] = []

    def NetUserGetInfo(self, server, user, level):  # noqa: N802 - pywin32 name
        if user == "ghost":
            raise RuntimeError("The user name could not be found.")
        if level == 1:
            return {"name": user, "flags": self.flags}
        return {"name": user, "logon_hours": self.hours}

    def NetUserSetInfo(self, server, user, level, info):  # noqa: N802
        self.set_calls.append((user, level, info))
        if level == 1008:
            self.flags = info["flags"]


def make(method="logon_hours", **kw):
    blocker = LogonBlocker.__new__(LogonBlocker)  # skip the Windows-only constructor
    blocker._net = FakeNet(**kw)
    blocker.method = method
    return blocker


@pytest.fixture
def net_exe(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        user = args[2]
        code = 2 if user == "ghost" else 0
        return subprocess.CompletedProcess(args, code, stdout="" if code == 0 else "The user name could not be found.", stderr="")

    monkeypatch.setattr(logonblock.subprocess, "run", fake_run)
    return calls


def test_block_sets_no_logon_hours_via_net_exe(net_exe):
    b = make()
    assert b.block("kevin") is True
    assert net_exe[-1][1:] == ["user", "kevin", "/times:"]
    assert net_exe[-1][0].lower().endswith("net.exe")
    assert b._net.set_calls == []  # the unsupported NetUserSetInfo level is no longer tried


def test_unblock_restores_all_hours(net_exe):
    b = make(hours=bytes(21))
    assert b.unblock("kevin") is True
    assert net_exe[-1][1:] == ["user", "kevin", "/times:all"]


def test_no_change_when_already_in_the_wanted_state(net_exe):
    assert make(hours=bytes(21)).block("kevin") is True
    assert make().unblock("kevin") is True
    assert net_exe == []


def test_unknown_user_reports_failure(net_exe, caplog):
    assert make().block("ghost") is False
    assert "could not block ghost" in caplog.text
    assert "information level" not in caplog.text


def test_net_exe_missing_reports_failure(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("net.exe")

    monkeypatch.setattr(logonblock.subprocess, "run", boom)
    assert make().block("kevin") is False


def test_is_blocked_reads_logon_hours():
    assert make(hours=bytes(21)).is_blocked("kevin") is True
    assert make().is_blocked("kevin") is False
    assert make(hours=None).is_blocked("kevin") is None
    assert make().is_blocked("ghost") is None


def test_disable_account_method_toggles_the_flag():
    b = make(method="disable_account", flags=0x201)
    assert b.block("kevin") is True
    assert b._net.flags & UF_ACCOUNTDISABLE
    assert b.is_blocked("kevin") is True
    assert b.unblock("kevin") is True
    assert not b._net.flags & UF_ACCOUNTDISABLE
    assert all(level == 1008 for _u, level, _i in b._net.set_calls)


def test_logoff_only_uses_the_null_blocker():
    blocker = make_blocker("logoff_only")
    assert isinstance(blocker, NullBlocker)
    assert blocker.block("kevin") and blocker.unblock("kevin") and blocker.is_blocked("kevin") is None
