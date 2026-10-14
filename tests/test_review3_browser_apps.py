"""Web app windows (PWA / --app) without an address bar: the agent reads the page's Document,
else reports the site as unknown; the service never takes an unknown site for an allowed one."""
import types

import yaml

from pclink.config import Config
from pclink.models import Activity, AgentReport
from pclink.rules import RuleEngine, focus_allows, rule_matches
from pclink.store import Store

from .test_ideas import NOW
from .test_ideas import make as make_orch
from .test_orchestrator import attach, report, session

CHROME_APP = "agimnkijcaahngcdmfeangaknmldooml"


# -- agent: pure helpers -----------------------------------------------------------------------------

def test_document_address_takes_web_addresses_only():
    from pclink.agent.browser_url import document_address

    assert document_address(" https://www.youtube.com/watch?v=1 ") == "https://www.youtube.com/watch?v=1"
    assert document_address("http://192.168.1.11:9091/web/") == "http://192.168.1.11:9091/web/"
    for value in ("devtools://devtools/bundled/x.html", "chrome://newtab/", "about:blank", "file:///C:/x.html",
                  "youtube.com", "", None, 5, "https://", "https://a b.com", "https://" + "a" * 3000):
        assert document_address(value) is None, value


def test_pick_document_needs_one_page_or_the_title():
    from pclink.agent.browser_url import pick_document

    assert pick_document(["Anything"], "") == 0                      # one document is the page
    assert pick_document([], "x") is None
    names = ["Inbox - Gmail", "Spam - Gmail", ""]
    assert pick_document(names, "Spam - Gmail — Mozilla Firefox") == 1
    assert pick_document(names, "Something else") is None
    assert pick_document(["Home", "Home"], "Home") is None            # two tabs with the same title: not told apart
    assert pick_document(["Mail", "Mail - Inbox"], "Mail - Inbox - Browser") == 1   # the most specific name
    assert pick_document(["A", "B"], "") is None


def test_app_launch_arguments_and_identity():
    from pclink.agent.browser_url import app_launch_args, app_window_identity

    args = ["C:\\chrome.exe", "--profile-directory=Default", "--app=https://www.youtube.com/feed", "--flag"]
    assert app_launch_args(args) == ("https://www.youtube.com/feed", None)
    assert app_launch_args(["chrome.exe", f"--app-id={CHROME_APP.upper()}"]) == (None, CHROME_APP)
    assert app_launch_args(["chrome.exe", "--app-id=../../etc", "--app=javascript:alert(1)"]) == (None, None)
    assert app_launch_args(None) == (None, None) and app_launch_args([1, None]) == (None, None)

    plain = app_window_identity(None, args)
    assert plain.start_url == "https://www.youtube.com/feed" and plain.app_id is None
    # an installed app's window in a process that was started for another --app: the address is not its own
    pwa = app_window_identity(f"Chrome._crx_{CHROME_APP}", args)
    assert pwa.start_url is None and pwa.app_id == f"Chrome._crx_{CHROME_APP}"
    assert app_window_identity(None, ["chrome.exe", f"--app-id={CHROME_APP}"]).app_id == CHROME_APP
    assert app_window_identity("bad id with spaces", []).app_id is None


def test_document_step_never_opens_the_page():
    from pclink.agent.browser_url import document_step

    assert document_step(50030) == "document"
    assert document_step(50021) == "skip"                            # a tool bar holds no page
    for container in (50033, 50032, 50026, 50025):
        assert document_step(container) == "expand"
    assert document_step(50004) == "skip" and document_step(None) == "skip" and document_step("x") == "skip"


# -- agent: the cache and the reader with fakes ----------------------------------------------------

def test_cache_remembers_app_windows_and_their_last_page():
    from pclink.agent.browser_url import READ_APP, READ_NONE, READ_URL, AppWindow, UrlCache

    c = UrlCache()
    app = AppWindow(app_id="Chrome._crx_x")
    c.record(7, "Video A", READ_URL, "https://www.youtube.com/watch?v=a", app)
    assert c.app_window(7) == app and c.current(7, "Video A").endswith("v=a")
    assert c.current(7, "Video B").endswith("v=a")       # an app window shows one page: kept until read again
    c.record(7, "Video B", READ_APP, None, app)
    assert c.current(7, "Video B").endswith("v=a")       # a failed read forgets nothing
    c.record(7, "Video B", READ_NONE)                    # an address bar (or no page) after all
    assert c.app_window(7) is None and c.current(7, "Video B") is None
    assert c.current(8, "Other") is None and c.app_window(8) is None   # ordinary windows: as before


def test_reader_reports_unknown_site_with_a_guess_only_for_block_lists(monkeypatch):
    from pclink.agent import browser_url as b

    reader = b.BrowserUrlReader("title")
    reader._take((3, "YouTube", b.READ_APP, None, b.AppWindow(start_url="https://www.youtube.com/feed")))
    site = reader.site(3, "YouTube")
    assert site.unknown and site.url is None and site.domain == "youtube.com" and site.host == "www.youtube.com"
    reader._take((4, "Some app", b.READ_APP, None, b.AppWindow(app_id="MSEdge._crx_y")))
    site = reader.site(4, "Some app")
    assert site.unknown and site.domain is None and site.app_id == "MSEdge._crx_y"
    reader._take((4, "Some app", b.READ_URL, "https://www.khanacademy.org/math", b.AppWindow(app_id="MSEdge._crx_y")))
    site = reader.site(4, "Some app")
    assert not site.unknown and site.domain == "khanacademy.org" and site.app_id == "MSEdge._crx_y"
    ordinary = reader.site(5, "news.example.com - Browser")
    assert not ordinary.unknown and ordinary.domain == "example.com"
    assert reader.url_and_domain(4, "Some app") == ("https://www.khanacademy.org/math", "khanacademy.org")


class Element:
    def __init__(self, ctype, name="", children=(), value=None):
        self.props = {30003: ctype, 30005: name, 30011: "", 30012: ""}
        self.children, self.value, self.opened = list(children), value, 0

    def GetCachedPropertyValue(self, prop_id):
        return self.props[prop_id]

    def GetCurrentPropertyValue(self, prop_id):
        return self.value if prop_id in (30045, 30093) else None

    def FindAllBuildCache(self, scope, condition, request):
        assert scope == 2                                                # children only
        self.opened += 1
        kids = self.children
        return types.SimpleNamespace(Length=len(kids), GetElement=lambda i: kids[i])

    def FindFirst(self, scope, condition):
        return None


def _uia(root, page=None):
    return types.SimpleNamespace(
        CreateCacheRequest=lambda: types.SimpleNamespace(AddProperty=lambda p: None),
        CreateTrueCondition=lambda: object(), CreatePropertyCondition=lambda p, v: (p, v),
        CreateOrCondition=lambda a, c: (a, c),
        ElementFromHandle=lambda hwnd: root if hwnd == 1 else page)


def test_app_window_is_read_from_its_document_only(monkeypatch):
    from pclink.agent import browser_url as b
    from pclink.agent import win_activity as w

    monkeypatch.setattr(w, "window_app_user_model_id", lambda hwnd: f"Chrome._crx_{CHROME_APP}")
    monkeypatch.setattr(w, "window_process_id", lambda hwnd: 0)
    monkeypatch.setattr(w, "child_window", lambda hwnd, cls: 0)
    inner = Element(50004, "Address bar")                                # a look-alike inside the page
    doc = Element(50030, "YouTube", [inner], value="https://www.youtube.com/watch?v=1")
    root = Element(50032, "YouTube", [Element(50033, "", [Element(50033, "", [doc])])])
    reader = b.BrowserUrlReader("title")
    outcome, url, app = reader._read_omnibox(_uia(root), 1)
    assert (outcome, url) == (b.READ_URL, "https://www.youtube.com/watch?v=1")
    assert app.app_id == f"Chrome._crx_{CHROME_APP}" and doc.opened == 0 and inner.opened == 0

    # no document readable, but a page window is there: the site is unknown
    doc.value = "chrome-error://chromewebdata/"
    assert reader._read_omnibox(_uia(root), 1)[:2] == (b.READ_APP, None)


def test_chromium_page_is_woken_then_read_and_dialogs_are_no_web_apps(monkeypatch):
    from pclink.agent import browser_url as b
    from pclink.agent import win_activity as w

    monkeypatch.setattr(w, "window_app_user_model_id", lambda hwnd: None)
    monkeypatch.setattr(w, "window_process_id", lambda hwnd: 0)
    monkeypatch.setattr(b, "DOCUMENT_SETTLE_SECONDS", 0.0)
    holder = Element(50033)
    root = Element(50032, "App", [holder])
    woken = []

    class Page:
        @property
        def CurrentName(self):
            woken.append(1)
            holder.children = [Element(50030, "App", value="https://school.example.org/")]
            return "Chrome Legacy Window"

    monkeypatch.setattr(w, "child_window", lambda hwnd, cls: 99 if cls == b.RENDER_WIDGET_CLASS else 0)
    reader = b.BrowserUrlReader("title")
    outcome, url, app = reader._read_omnibox(_uia(root, Page()), 1)
    assert woken == [1] and outcome == b.READ_URL and url == "https://school.example.org/"

    # a window of the browser without any web page (a Save as dialog): not a web app
    monkeypatch.setattr(w, "child_window", lambda hwnd, cls: 0)
    assert reader._read_omnibox(_uia(Element(50032, "Save as", [Element(50033)])), 1) == (b.READ_NONE, None)


def test_agent_report_carries_the_fields():
    from pclink.agent.main import _REPORT_FIELDS

    assert {"site_unknown", "app_id"} <= _REPORT_FIELDS


# -- service: the report ------------------------------------------------------------------------------

def test_report_fields_are_validated_and_optional():
    old = AgentReport.from_dict({"session_id": 2, "ts": 1.0, "domain": "x.com"})
    assert old.invalid is None and old.site_unknown is False and old.app_id is None
    new = AgentReport.from_dict({"session_id": 2, "ts": 1.0, "site_unknown": True, "app_id": "  Chrome._crx_a\x00b " + "z" * 500})
    assert new.invalid is None and new.site_unknown is True
    assert new.app_id.startswith("Chrome._crx_a b") and len(new.app_id) <= 128
    for bad in ({"site_unknown": "yes"}, {"app_id": 5}, {"site_unknown": [1]}):
        assert AgentReport.from_dict({"session_id": 2, "ts": 1.0, **bad}).invalid
    assert AgentReport(session_id=1, ts=0, site_unknown=True).to_dict()["site_unknown"] is True
    assert AgentReport(session_id=1, ts=0).signature() != AgentReport(session_id=1, ts=0, site_unknown=True).signature()


# -- service: rules and focus ------------------------------------------------------------------------

YAML = """
time_limits:
  users:
    - name: kevin
      daily_limit_minutes: 120
      rules:
        - name: YouTube
          sites: [youtube.com]
          blocked: true
      focus:
        allow_apps: [winword.exe, chrome.exe]
        allow_sites: [khanacademy.org]
        allow_categories: [education, browsing]
    - name: anna
      daily_limit_minutes: 120
      focus:
        allow_sites: [khanacademy.org]
        allow_unknown_sites: true
"""


def unknown(domain=None, url=None, category="browsing"):
    return Activity(category=category, app="chrome.exe", domain=domain, url=url, site_unknown=True, app_id="Chrome._crx_x")


def test_focus_never_allows_an_unknown_site():
    cfg = Config.from_dict(yaml.safe_load(YAML))
    focus = cfg.user_policy("kevin").focus
    assert focus.allow_unknown_sites is False
    # the browser itself and "browsing" are allowed, a readable allowed site too ...
    assert focus_allows(focus, Activity(category="browsing", app="chrome.exe", domain="khanacademy.org"))
    assert focus_allows(focus, Activity(category="browsing", app="chrome.exe"))       # no site known (old agent)
    # ... but a web app whose address could not be read is not, whatever it guesses
    assert not focus_allows(focus, unknown())
    assert not focus_allows(focus, unknown("khanacademy.org", category="education"))
    assert not focus_allows(focus, unknown(), extra_allow=["chrome.exe", "khanacademy.org"])
    assert focus_allows(cfg.user_policy("anna").focus, unknown())                      # the parent allowed them


def test_unknown_site_is_not_blocked_but_a_guess_is_and_closes_like_a_tab():
    cfg = Config.from_dict(yaml.safe_load(YAML))
    policy = cfg.user_policy("kevin")
    youtube = next(r for r in policy.rules if r.slug == "youtube")
    assert not rule_matches(youtube, unknown())
    assert rule_matches(youtube, unknown("youtube.com"))
    eng = RuleEngine(Store(":memory:"))
    decisions = eng.tick("kevin", policy, NOW, unknown("youtube.com"))
    assert decisions and decisions[0].close_tab and decisions[0].kind == "close"
    focus = eng.tick("kevin", policy, NOW, unknown(), focus_active=True)
    assert [d.source for d in focus] == ["focus"] and focus[0].close_tab        # Ctrl+W, not the whole browser
    assert not eng.tick("kevin", policy, NOW, unknown(), focus_active=True, unknown_site_pending=True)
    # the wait is only for unknown sites: a known, not allowed one is acted on at once
    other = Activity(category="video", app="chrome.exe", domain="twitch.tv")
    assert eng.tick("kevin", policy, NOW, other, focus_active=True, unknown_site_pending=True)


def _focus_orch():
    orch, sessions, api, agents = make_orch(YAML.replace("time_limits:", "device: {name: TestPC}\ntelemetry: {interval_seconds: 0}\ntime_limits:", 1))
    sessions.sessions = [session()]
    agents.connected.add(2)
    mono = {"t": 1000.0}
    orch._mono = lambda: mono["t"]
    return orch, sessions, api, agents, mono


def _closes(agents):
    return [m for _sid, m in agents.sent if m.get("type") == "close"]


def test_service_closes_an_unknown_web_app_in_focus_once_it_stays_unknown():
    from pclink.core import UNKNOWN_SITE_CONFIRM_SECONDS

    orch, sessions, api, agents, mono = _focus_orch()
    attach(orch, sessions, 2, report(title="YouTube", site_unknown=True, app_id="Chrome._crx_x"))
    orch.tick()
    orch._holder["now"] = NOW.replace(minute=1)
    mono["t"] += 60
    orch.tick()
    assert api.users["kevin"]["activity_details"]["site_unknown"] is True
    assert orch.store.usage("kevin", NOW.date()).browsing > 0                      # counted as browsing
    assert not _closes(agents)                                                      # no rule blocks it
    assert orch.api_command({"action": "focus", "user": "kevin", "minutes": 10})["ok"]
    orch._holder["now"] = NOW.replace(minute=2)
    mono["t"] += 5
    orch.tick()
    assert not _closes(agents)                     # just turned unknown under focus: the agent may still read it
    attach(orch, sessions, 2, report(title="YouTube - a video", site_unknown=True, app_id="Chrome._crx_x"))
    mono["t"] += UNKNOWN_SITE_CONFIRM_SECONDS
    orch.tick()                                    # the same app window (a new title is no new window) stayed unknown
    closes = _closes(agents)
    assert closes and closes[-1]["tab"] is True and closes[-1]["name"] == "chrome.exe"


def test_unknown_at_first_then_an_allowed_site_is_not_closed():
    from pclink.core import UNKNOWN_SITE_CONFIRM_SECONDS

    orch, sessions, api, agents, mono = _focus_orch()
    # kevin's focus allows khanacademy.org (and chrome.exe itself, which does not cover unknown sites)
    assert orch.api_command({"action": "focus", "user": "kevin", "minutes": 30})["ok"]
    attach(orch, sessions, 2, report(title="Khan Academy", site_unknown=True, app_id="Chrome._crx_k"))
    orch.tick()
    assert not _closes(agents)                     # the first read of the web app failed or came late
    mono["t"] += 2
    attach(orch, sessions, 2, report(title="Khan Academy", domain="khanacademy.org",
                                     url="https://www.khanacademy.org/math", app_id="Chrome._crx_k"))
    orch.tick()
    mono["t"] += UNKNOWN_SITE_CONFIRM_SECONDS * 3
    orch.tick()
    assert not _closes(agents)                     # read in time: an allowed site, never closed
    # unknown again later: the wait starts afresh rather than counting from the first time
    attach(orch, sessions, 2, report(title="Khan Academy", site_unknown=True, app_id="Chrome._crx_k"))
    orch.tick()
    assert not _closes(agents)


# -- the orchestrator's clock is the time keeper's ---------------------------------------------------

def test_time_keeper_uses_the_orchestrators_clock():
    orch = make_orch()[0]
    assert orch.timekeeper.clock() == NOW
    orch._holder["now"] = NOW.replace(hour=16)
    assert orch.timekeeper.clock() == NOW.replace(hour=16)
