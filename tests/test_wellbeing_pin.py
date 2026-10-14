from datetime import date

from pclink.config import BreakConfig, TimeLimitsConfig
from pclink.pin import ParentPin, hash_pin
from pclink.wellbeing import BreakReminder


def _use(br, cfg, start, end, day, step=30.0):
    """Tick (engaged) every ``step`` seconds after ``start`` up to before ``end``, as the
    service does: a hole of minutes between two ticks is sleep, which is a break."""
    t = start + step
    while t < end:
        assert br.tick("kevin", cfg, t, day, engaged=True) is None
        t += step


def test_break_reminder_fires_after_continuous_use_and_repeats():
    br = BreakReminder()
    cfg = BreakConfig(enabled=True, every_minutes=20, duration_seconds=20)
    day = date(2026, 9, 28)
    t0 = 1_000_000.0
    assert br.tick("kevin", cfg, t0, day, engaged=True) is None
    _use(br, cfg, t0, t0 + 19 * 60, day)
    assert br.tick("kevin", cfg, t0 + 19 * 60, day, engaged=True) is None
    r = br.tick("kevin", cfg, t0 + 20 * 60, day, engaged=True)
    assert r and r["continuous_minutes"] == 20 and r["reminders_today"] == 1
    assert br.tick("kevin", cfg, t0 + 21 * 60, day, engaged=True) is None
    _use(br, cfg, t0 + 21 * 60, t0 + 40 * 60, day)
    r2 = br.tick("kevin", cfg, t0 + 40 * 60, day, engaged=True)
    assert r2 and r2["reminders_today"] == 2
    st = br.status("kevin", cfg, t0 + 41 * 60)
    assert st["continuous_minutes"] == 41 and st["next_break_in_minutes"] == 19


def test_break_resets_after_real_pause_but_not_a_blink():
    br = BreakReminder()
    cfg = BreakConfig(enabled=True, every_minutes=20, duration_seconds=20)
    day = date(2026, 9, 28)
    t0 = 1_000_000.0
    br.tick("kevin", cfg, t0, day, engaged=True)
    _use(br, cfg, t0, t0 + 10 * 60, day)
    br.tick("kevin", cfg, t0 + 10 * 60, day, engaged=True)
    # a 10-second glance away is not a break
    br.tick("kevin", cfg, t0 + 10 * 60 + 5, day, engaged=False)
    br.tick("kevin", cfg, t0 + 10 * 60 + 10, day, engaged=False)
    br.tick("kevin", cfg, t0 + 10 * 60 + 15, day, engaged=True)
    assert br.status("kevin", cfg, t0 + 10 * 60 + 15)["continuous_minutes"] > 9
    # a 30-second pause is
    br.tick("kevin", cfg, t0 + 11 * 60, day, engaged=False)
    br.tick("kevin", cfg, t0 + 11 * 60 + 30, day, engaged=False)
    assert br.status("kevin", cfg, t0 + 11 * 60 + 30)["continuous_minutes"] == 0
    assert br.status("kevin", cfg, t0 + 11 * 60 + 30)["breaks_taken_today"] == 1
    br.tick("kevin", cfg, t0 + 12 * 60, day, engaged=True)
    assert br.status("kevin", cfg, t0 + 12 * 60)["continuous_minutes"] == 0


def test_break_disabled_never_fires():
    br = BreakReminder()
    cfg = BreakConfig(enabled=True, every_minutes=1)
    day = date(2026, 9, 28)
    assert br.tick("kevin", cfg, 0, day, engaged=True, enabled=False) is None
    assert br.tick("kevin", cfg, 120, day, engaged=True, enabled=False) is None
    cfg2 = BreakConfig(enabled=False, every_minutes=1)
    assert br.tick("kevin", cfg2, 0, day, engaged=True) is None
    assert br.tick("kevin", cfg2, 120, day, engaged=True) is None


def make_pin(**over):
    cfg = TimeLimitsConfig(parent_pin="1234", pin_timeout_seconds=300, pin_max_attempts=3, pin_lockout_seconds=600)
    for k, v in over.items():
        setattr(cfg, k, v)
    clock = {"t": 1000.0}
    events = []
    pin = ParentPin(cfg, clock=lambda: clock["t"], on_event=events.append)
    return pin, clock, events


def test_pin_gate_basics():
    pin, clock, events = make_pin()
    assert pin.enabled and pin.requires("grant") and not pin.unlocked()
    assert pin.authorize("grant") == (False, "parent pin required")
    assert pin.authorize("grant", pin="9999") == (False, "wrong pin")
    assert pin.authorize("grant", pin="1234") == (True, "")
    # a PIN that comes with a command is for that command: nothing stays unlocked
    assert not pin.unlocked() and pin.authorize("unblock") == (False, "parent pin required")
    assert pin.try_unlock("1234")
    assert pin.unlocked() and pin.authorize("unblock") == (True, "")
    clock["t"] += 301
    assert not pin.unlocked() and pin.authorize("grant")[0] is False
    assert pin.authorize("tighten")[0] is True  # kinds not in the list never need a PIN
    assert "parent_unlocked" in events and "pin_wrong" in events


def test_pin_lockout_after_failures():
    pin, clock, events = make_pin()
    for _ in range(3):
        assert not pin.try_unlock("0000")
    assert pin.locked_out()
    assert not pin.try_unlock("1234")  # even the right PIN is refused while locked out
    assert pin.authorize("grant", pin="1234") == (False, "locked out")
    clock["t"] += 601
    assert pin.try_unlock("1234")


def test_pin_hash_config_and_disabled():
    cfg = TimeLimitsConfig(parent_pin_sha256=hash_pin("4321"))
    pin = ParentPin(cfg)
    assert pin.try_unlock("4321") and not pin.try_unlock("1234")
    off = ParentPin(TimeLimitsConfig())
    assert not off.enabled and off.authorize("grant") == (True, "")
    st = pin.status()
    assert st["enabled"] and "grant" in st["required_for"]


def test_break_streak_does_not_span_a_sleep_gap_or_a_clock_step():
    br = BreakReminder()
    cfg = BreakConfig(enabled=True, every_minutes=20, duration_seconds=20)
    day = date(2026, 9, 28)
    t0 = 1_000_000.0
    br.tick("kevin", cfg, t0, day, engaged=True)
    br.tick("kevin", cfg, t0 + 5, day, engaged=True)
    # the PC slept for three hours: that was a break, not three hours of screen time
    assert br.tick("kevin", cfg, t0 + 3 * 3600, day, engaged=True) is None
    st = br.status("kevin", cfg, t0 + 3 * 3600)
    assert st["continuous_minutes"] == 0 and st["breaks_taken_today"] == 1
    # the clock is set back an hour: the streak restarts from "now" instead of going negative
    back = t0 + 2 * 3600
    assert br.tick("kevin", cfg, back, day, engaged=True) is None
    _use(br, cfg, back, back + 19 * 60, day)
    assert br.tick("kevin", cfg, back + 19 * 60, day, engaged=True) is None
    assert br.tick("kevin", cfg, back + 20 * 60, day, engaged=True)
    # a streak that runs over midnight is kept; only the counters restart
    nxt = date(2026, 9, 29)
    _use(br, cfg, back + 20 * 60, back + 25 * 60, nxt)
    assert br.tick("kevin", cfg, back + 25 * 60, nxt, engaged=True) is None
    st = br.status("kevin", cfg, back + 25 * 60)
    assert st["continuous_minutes"] == 25 and st["reminders_today"] == 0


def test_pin_lockout_survives_a_restart_when_given_a_store():
    from pclink.store import Store

    store = Store(":memory:")
    cfg = TimeLimitsConfig(parent_pin="1234", pin_max_attempts=3, pin_lockout_seconds=600)
    clock = {"t": 100_000.0}
    pin = ParentPin(cfg, clock=lambda: clock["t"], store=store)
    assert not pin.try_unlock("0000") and not pin.try_unlock("0000")
    # "restart": two failures are remembered, the third locks out
    pin = ParentPin(cfg, clock=lambda: clock["t"], store=store)
    assert not pin.try_unlock("0000") and pin.locked_out()
    pin = ParentPin(cfg, clock=lambda: clock["t"], store=store)
    assert pin.locked_out() and not pin.try_unlock("1234")
    assert pin.status()["lockout_seconds_left"] == 600
    # a clock set back does not stretch the lockout beyond one full period
    clock["t"] -= 5000
    assert pin.status()["lockout_seconds_left"] == 600
    clock["t"] += 601
    assert pin.try_unlock("1234") and not pin.locked_out()
    # a success clears the stored state
    assert ParentPin(cfg, clock=lambda: clock["t"], store=store).status()["locked_out"] is False
    assert store.get_setting(Store.GLOBAL, "pin_failures") == 0
