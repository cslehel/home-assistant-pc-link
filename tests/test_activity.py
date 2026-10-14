from pclink.activity import ActivityClassifier
from pclink.config import ActivityConfig
from pclink.models import AgentReport, ForegroundInfo


def clf(**over):
    cfg = ActivityConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return ActivityClassifier(cfg)


def report(name="", exe="", title="", domain=None, idle=0.0, locked=False, fullscreen=False, audio=False):
    fg = ForegroundInfo(pid=100, name=name, exe=exe, title=title, fullscreen=fullscreen) if name else None
    return AgentReport(session_id=1, ts=0, idle_seconds=idle, locked=locked, foreground=fg, domain=domain, audio_active=audio)


def test_locked_and_idle():
    c = clf()
    assert c.classify(report(locked=True)).category == "locked"
    assert c.classify(report(name="chrome.exe", idle=999)).category == "idle"
    assert c.classify(None).category == "none"


def test_audio_keeps_active_when_idle():
    c = clf(audio_keeps_active=True)
    act = c.classify(report(name="chrome.exe", domain="youtube.com", idle=999, audio=True))
    assert act.category != "idle"


def test_browser_becomes_site_category():
    c = clf(site_categories={"youtube.com": "video"})
    act = c.classify(report(name="chrome.exe", domain="youtube.com", title="x - YouTube"))
    assert act.category == "video" and act.domain == "youtube.com"
    act2 = c.classify(report(name="chrome.exe", domain="example.com"))
    assert act2.category == "browsing"


def test_game_detection_by_path_and_fullscreen():
    c = clf()
    act = c.classify(report(name="game.exe", exe="C:\\Games\\steamapps\\common\\Foo\\game.exe"))
    assert act.category == "gaming"
    # fullscreen unknown app -> heuristic game
    act2 = c.classify(report(name="unknownfps.exe", fullscreen=True))
    assert act2.category == "gaming"
    # fullscreen browser is NOT a game
    act3 = c.classify(report(name="chrome.exe", domain="youtube.com", fullscreen=True))
    assert act3.category != "gaming"


def test_known_game_by_name():
    c = clf(games=["javaw.exe"])
    assert c.classify(report(name="javaw.exe", title="Minecraft")).category == "gaming"


def test_regular_app():
    c = clf()
    assert c.classify(report(name="code.exe", title="editor")).category == "app"


def test_passive_use_is_not_idle():
    from pclink.models import MediaInfo
    c = clf()
    r = report(name="chrome.exe", domain="youtube.com", idle=999)
    assert c.classify(r).category == "idle"
    # the caller's presence verdict wins ...
    assert c.classify(r, presence="passive").category == "browsing"
    assert c.classify(r, presence="idle").category == "idle"
    # ... and without one, a playing video / full-screen app is still "in use"
    r.media = MediaInfo(title="A film", status="playing")
    assert c.classify(r).category == "browsing" and c.classify(r).domain == "youtube.com"
    assert c.classify(r, presence="idle").category == "idle"
    r.media = None
    r.fullscreen_d3d = True
    assert c.classify(r).is_active
    assert clf(media_keeps_active=False).classify(r).category == "idle"
