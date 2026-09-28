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
