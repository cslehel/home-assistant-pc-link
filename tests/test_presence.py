from pclink.config import ActivityConfig
from pclink.models import AgentReport, ForegroundInfo, MediaInfo, SessionInfo
from pclink.presence import best, evaluate, user_state


def make(idle=0.0, locked=False, state="active", report=True, **rep):
    r = None
    if report:
        r = AgentReport(session_id=1, ts=0, idle_seconds=idle, locked=locked, **rep)
    return SessionInfo(session_id=1, user="kevin", state=state, locked=locked, idle_seconds=idle, report=r)


def test_recent_input_is_active():
    p = evaluate(make(idle=10), ActivityConfig())
    assert p.state == "active" and p.engaged


def test_no_input_is_idle_then_away():
    cfg = ActivityConfig(idle_seconds=120, away_seconds=600)
    assert evaluate(make(idle=200), cfg).state == "idle"
    assert evaluate(make(idle=700), cfg).state == "away"


def test_locked_and_disconnected_are_away():
    assert evaluate(make(idle=0, locked=True), ActivityConfig()).state == "away"
    assert evaluate(make(state="disconnected"), ActivityConfig()).state == "away"


def test_audio_makes_passive_not_idle():
    cfg = ActivityConfig()
    p = evaluate(make(idle=500, audio_active=True), cfg)
    assert p.state == "passive" and p.engaged and "audio" in p.reason
    # unless audio is configured not to count
    cfg.audio_keeps_active = False
    assert evaluate(make(idle=500, audio_active=True), cfg).state == "idle"


def test_fullscreen_video_keeps_seat_passive():
    cfg = ActivityConfig()
    fg = ForegroundInfo(name="vlc.exe", fullscreen=True)
    p = evaluate(make(idle=900, foreground=fg, display_required=True), cfg)
    assert p.state == "passive"
    # display-required by some unrelated background app does not count when the foreground is a plain app
    fg2 = ForegroundInfo(name="notepad.exe")
    p2 = evaluate(make(idle=900, foreground=fg2, display_required=True), cfg)
    assert p2.state == "away"
    # exclusive full-screen D3D (a game) counts regardless of the foreground name
    assert evaluate(make(idle=900, fullscreen_d3d=True), cfg).state == "passive"


def test_now_playing_counts_as_passive():
    media = MediaInfo(title="Song", artist="Band", status="playing")
    p = evaluate(make(idle=400, media=media), ActivityConfig())
    assert p.state == "passive" and "Band - Song" in p.reason


def test_monitor_off_is_away_even_when_audio_plays_but_only_after_min_idle():
    cfg = ActivityConfig(display_off_min_idle_seconds=30)
    # display just turned off (another seat's monitor, say) but there was input 5 s ago -> still active
    assert evaluate(make(idle=5, display_state="off"), cfg).state == "active"
    assert evaluate(make(idle=45, display_state="off", audio_active=True), cfg).state == "away"
    assert evaluate(make(idle=200, display_state="off", audio_active=True), cfg).state == "away"
    cfg.display_off_is_idle = False
    assert evaluate(make(idle=200, display_state="off", audio_active=True), cfg).state == "passive"


def test_screensaver_is_away():
    assert evaluate(make(idle=200, screensaver=True), ActivityConfig()).state == "away"


def test_windows_presence_verdict_speeds_up_idle():
    cfg = ActivityConfig(idle_seconds=300)
    # our threshold not reached, but Windows already says the user is gone and it has been > 60 s
    p = evaluate(make(idle=90, user_present=False), cfg)
    assert p.state == "idle" and "windows" in p.reason
    # very recent input beats Windows' opinion
    assert evaluate(make(idle=20, user_present=False), cfg).state == "active"
    cfg.windows_presence = False
    assert evaluate(make(idle=90, user_present=False), cfg).state == "active"


def test_controller_input_counts_as_input():
    cfg = ActivityConfig()
    # the agent folds controller input into idle_seconds; the reason reflects it
    p = evaluate(make(idle=3, input_idle_seconds=500, controller_idle_seconds=3), cfg)
    assert p.state == "active" and p.reason == "controller input"


def test_without_agent_falls_back_to_wts_idle():
    cfg = ActivityConfig(idle_seconds=120, away_seconds=600)
    assert evaluate(make(idle=10, report=False), cfg).state == "active"
    assert evaluate(make(idle=200, report=False), cfg).state == "idle"
    assert evaluate(make(idle=1000, report=False), cfg).state == "away"
    s = make(idle=None, report=False)
    assert evaluate(s, cfg).state == "active"


def test_threshold_overrides():
    cfg = ActivityConfig(idle_seconds=120, away_seconds=600)
    assert evaluate(make(idle=200), cfg, idle_threshold=600).state == "active"
    assert evaluate(make(idle=700), cfg, idle_threshold=600, away_threshold=900).state == "idle"


def test_best_and_user_state():
    a = evaluate(make(idle=10), ActivityConfig())
    b = evaluate(make(idle=900), ActivityConfig())
    assert best([b, a]).state == "active"
    s1 = make(idle=10)
    s1.presence = "idle"
    s2 = make(idle=10)
    s2.presence = "passive"
    assert user_state([s1, s2]) == "passive"
    assert user_state([]) == "none"
