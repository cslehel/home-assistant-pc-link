"""Best-effort active-tab URL from a browser's address bar via UI Automation.

Reading the omnibox with UI Automation is read-only and does not inject into
the browser, which keeps antivirus and the browsers themselves happy.  It is
throttled hard: the address bar is read when the foreground window or its
title changes, and again every few seconds to notice a page that changed
without changing its title.  The read itself runs on a helper thread with a
deadline, so a browser that does not answer can never stop the agent's poll.
When ``comtypes`` / UIAutomation is missing, the domain is derived from the
window title instead, and failing that it is simply omitted.

A browser window without an address bar - an installed web app (PWA) or an
``--app`` window - is read differently: the page's top-level Document element
carries the address as its value (Chromium and Firefox both expose it).  Only
that one element is read, never anything inside the page.  When it cannot be
read either, the window is reported as a web page whose site is unknown
(``site_unknown``), with the app's identity when Windows or the browser's
command line gives one; the service never takes such a window for an
allow-listed site.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..util import domain_from_url

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"
_TITLE_DOMAIN_RE = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)

# UIAutomation control type ids
_UIA_EDIT = 50004
_UIA_COMBOBOX = 50003
_UIA_VALUE_PROPERTY = 30045
_UIA_CONTROLTYPE_PROPERTY = 30003
_UIA_NAME_PROPERTY = 30005
_UIA_HASKEYBOARDFOCUS_PROPERTY = 30008
_UIA_AUTOMATIONID_PROPERTY = 30011
_UIA_CLASSNAME_PROPERTY = 30012
# containers a browser's own user interface is made of.  A web page is a Document
# (50030): it is never entered, however large it is.
_UIA_CONTAINERS = {50033, 50032, 50021, 50026, 50025}   # pane, window, tool bar, group, custom

MIN_WALK_SECONDS = 4.0    # the address bar of one window is read at most this often
RECHECK_SECONDS = 12.0    # ... and at least this often while the window stays in front
FAST_WAIT_SECONDS = 0.6   # how long one poll waits for a read; a slower one is picked up by a later poll
ABANDON_SECONDS = 20.0    # a read that takes longer than this is given up (the browser hangs)
MAX_ABANDONED = 3         # after this many hangs (or MAX_ERRORS failures in a row) UI Automation rests ...
MAX_ERRORS = 20
COOLDOWN_SECONDS = 600.0  # ... for this long, doubled after each rest that did not help ...
MAX_COOLDOWN_SECONDS = 4 * 3600.0   # ... up to this; websites come from window titles meanwhile
MAX_EXPANDED = 60         # containers opened per toolbar search
MAX_CHILDREN = 200        # children looked at per container
_UIA_TOOLBAR = 50021
_UIA_DOCUMENT = 50030
_UIA_LEGACY_VALUE_PROPERTY = 30093
MAX_DOCUMENTS = 64        # documents (tabs) collected per search; only the chosen one is read
DOCUMENT_SETTLE_SECONDS = 0.3   # after waking a Chromium page's accessibility, before looking again
MAX_APP_WINDOWS = 64      # windows without an address bar remembered at once
# The web content of a Chromium window is drawn in a child window of this class.  Chromium
# exposes the page's Document element only once something asked this window for it.
RENDER_WIDGET_CLASS = "Chrome_RenderWidgetHostHWND"
_DOCUMENT_URL_RE = re.compile(r"^https?://[^\s/?#]+[^\s]*$", re.I)
_CHROME_APP_ID_RE = re.compile(r"^[a-p]{32}$")
_APP_ID_RE = re.compile(r"^[A-Za-z0-9._\-!:{}@]{1,128}$")


def cooldown_seconds(rests: int, base: float = COOLDOWN_SECONDS, cap: float = MAX_COOLDOWN_SECONDS) -> float:
    """How long UI Automation rests after its ``rests``-th failure streak (1, 2, ...) (pure; unit-tested)."""
    return float(min(cap, base * (2 ** max(0, min(int(rests) - 1, 16)))))

# How the address bar identifies itself (locale-independent ids first):
#   Chromium family (Chrome, Edge, Brave, Opera, Vivaldi): class "OmniboxViewViews"
#   Firefox: automation id "urlbar-input"
# The accessible name is a last hint only, since it is translated.
_OMNIBOX_CLASS = "OmniboxViewViews"
_FIREFOX_URLBAR_ID = "urlbar-input"
_ADDRESS_CLASSES = {"omniboxviewviews"}
_ADDRESS_IDS = {"urlbar-input", "urlbar", "addresseditbox"}
_ADDRESS_NAME_HINTS = ("address and search bar", "address bar", "search or enter address",
                       "search with google or enter address")

# outcome of one read of the address bar
READ_URL = "url"          # the committed address
READ_TYPING = "typing"    # the bar has keyboard focus: what it shows is being typed, not visited
READ_NONE = "none"        # no address bar, or nothing in it that is an address
READ_APP = "app"          # a web page window without an address bar whose address could not be read
READ_ERROR = "error"


@dataclass(frozen=True)
class AppWindow:
    """A browser window without an address bar (an installed web app, an ``--app`` window)."""

    app_id: Optional[str] = None      # the window's AppUserModelID, else the browser's --app-id
    start_url: Optional[str] = None   # the --app= address the browser was started with: a guess only


@dataclass(frozen=True)
class SiteInfo:
    """What the agent reports about the page in a browser window."""

    url: Optional[str] = None
    domain: Optional[str] = None
    host: Optional[str] = None
    unknown: bool = False             # a web page window whose address could not be read
    app_id: Optional[str] = None


def document_address(value: Any) -> Optional[str]:
    """The value of a page's Document element when it is a web address (pure; unit-tested).

    Only http(s) addresses count: an internal page (devtools://, chrome://,
    about:, file://) is no website."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2048 or not _DOCUMENT_URL_RE.match(value):
        return None
    return value


def pick_document(names: list[str], title: str) -> Optional[int]:
    """Which of the Documents found in a window is the page in front (pure; unit-tested).

    One document is the page.  With several (Firefox keeps one per tab, a
    Chromium window may hold a side panel) the one whose name - the page's
    title - is in the window title; None when that does not single one out."""
    if len(names) == 1:
        return 0
    title = (title or "").strip()
    if not title:
        return None
    matches = [i for i, name in enumerate(names) if (name or "").strip() and name.strip() in title]
    if len(matches) > 1:
        longest = max(len(names[i].strip()) for i in matches)
        matches = [i for i in matches if len(names[i].strip()) == longest]
    return matches[0] if len(matches) == 1 else None


def app_launch_args(args: Any) -> tuple[Optional[str], Optional[str]]:
    """(``--app=`` address, ``--app-id=``) from a Chromium browser's command line (pure; unit-tested)."""
    start_url = app_id = None
    if not isinstance(args, (list, tuple)):
        return None, None
    for arg in args[:256]:
        if not isinstance(arg, str):
            continue
        low = arg.lower()
        if low.startswith("--app=") and start_url is None:
            start_url = document_address(arg[6:])
        elif low.startswith("--app-id=") and app_id is None:
            value = arg[9:].strip().lower()
            app_id = value if _CHROME_APP_ID_RE.match(value) else None
    return start_url, app_id


def app_window_identity(aumid: Optional[str], args: Any) -> AppWindow:
    """The identity of a window without an address bar (pure; unit-tested).

    The start address of the browser process is used only when nothing says the
    window belongs to an installed app: the process a browser was first started
    in (``--app=...``) also shows every window opened later, so for an installed
    app's window the address would be another app's."""
    start_url, arg_id = app_launch_args(args)
    aumid = (aumid or "").strip()
    aumid = aumid if aumid and _APP_ID_RE.match(aumid) else ""
    installed = "_crx_" in aumid.lower() or arg_id is not None
    return AppWindow(app_id=aumid or arg_id, start_url=None if installed else start_url)


def document_step(control_type: Any) -> str:
    """What the document search does with one element: "document", "expand" or "skip" (pure; unit-tested).

    A Document is taken as it is and never opened; tool bars hold no page."""
    try:
        ctype = int(control_type)
    except (TypeError, ValueError):
        return "skip"
    if ctype == _UIA_DOCUMENT:
        return "document"
    return "expand" if ctype in _UIA_CONTAINERS and ctype != _UIA_TOOLBAR else "skip"


def is_address_bar(automation_id: str, class_name: str, name: str) -> bool:
    """True when a UI Automation edit control is a browser's address bar (pure; unit-tested)."""
    if (class_name or "").strip().lower() in _ADDRESS_CLASSES:
        return True
    if (automation_id or "").strip().lower() in _ADDRESS_IDS:
        return True
    lname = (name or "").strip().lower()
    return bool(lname) and any(hint in lname for hint in _ADDRESS_NAME_HINTS)


def looks_like_address(value: str) -> bool:
    """A cheap pre-filter: an address has a dot or a slash and no spaces (typed search text does)."""
    value = (value or "").strip()
    return bool(value) and " " not in value and ("." in value or "/" in value)


def toolbar_step(control_type: Any, automation_id: str, class_name: str, name: str) -> str:
    """What the toolbar search does with one element: "match", "expand" or "skip" (pure; unit-tested).

    Only containers of the browser's own interface are opened.  Documents (the
    page), tabs, lists and buttons are not, so the search stays inside the
    toolbar area whatever the page contains.
    """
    try:
        ctype = int(control_type)
    except (TypeError, ValueError):
        ctype = 0
    if ctype in (_UIA_EDIT, _UIA_COMBOBOX) and is_address_bar(automation_id, class_name, name):
        return "match"
    if (class_name or "").strip().lower() in _ADDRESS_CLASSES or (automation_id or "").strip().lower() == _FIREFOX_URLBAR_ID:
        return "match"
    return "expand" if ctype in _UIA_CONTAINERS else "skip"


class UrlCache:
    """When to read the address bar and what to report meanwhile (pure logic; unit-tested).

    * a new window or title is read at once (but one window not more often
      than every ``MIN_WALK_SECONDS``: page titles can tick);
    * the same window and title is read again every ``RECHECK_SECONDS`` - a
      site may change the page without changing the title;
    * a read that found nothing is not final: it is repeated after
      ``MIN_WALK_SECONDS``, and the last address known for this very window
      and title stays in force until then;
    * while the user types in the address bar the last committed address of
      this window and title is kept; the typed text is never reported.
    * a window without an address bar (a web app) is remembered as one, and its
      last page address stays in force when its title changes, until it was
      read again: such a window shows one page, not tabs that could be switched.
    """

    def __init__(self):
        self.key: Optional[tuple[int, str]] = None   # (hwnd, title) the committed address belongs to
        self.url: Optional[str] = None
        self._asked_key: Optional[tuple[int, str]] = None
        self._asked_at = float("-inf")
        self._asked_hwnd = 0
        self._last_ok = False
        self._apps: dict[int, AppWindow] = {}         # windows without an address bar (oldest first)
        self._app_urls: dict[int, str] = {}           # ... and the last address read in each

    def due(self, hwnd: int, title: str, now: float) -> bool:
        key = (hwnd, title)
        age = now - self._asked_at
        if key != self._asked_key:
            # another window is read at once; the same window with a new title not too often
            return hwnd != self._asked_hwnd or age >= MIN_WALK_SECONDS
        return age >= (RECHECK_SECONDS if self._last_ok else MIN_WALK_SECONDS)

    def asked(self, hwnd: int, title: str, now: float) -> None:
        self._asked_key, self._asked_hwnd, self._asked_at = (hwnd, title), hwnd, now
        self._last_ok = False

    def record(self, hwnd: int, title: str, outcome: str, value: Optional[str] = None,
               app: Optional[AppWindow] = None) -> None:
        key = (hwnd, title)
        if app is not None:
            self._apps.pop(hwnd, None)
            self._apps[hwnd] = app
            if len(self._apps) > MAX_APP_WINDOWS:
                oldest = next(iter(self._apps))
                self._apps.pop(oldest, None)
                self._app_urls.pop(oldest, None)
            if outcome == READ_URL and value:
                self._app_urls[hwnd] = value
        elif outcome in (READ_URL, READ_TYPING, READ_NONE):
            # an address bar after all (a full-screen page is shown with its tool bar again),
            # or no web page in the window
            self._apps.pop(hwnd, None)
            self._app_urls.pop(hwnd, None)
        if outcome == READ_URL and value:
            self.key, self.url = key, value
            if key == self._asked_key:
                self._last_ok = True
        elif outcome == READ_TYPING:
            if key == self._asked_key:
                self._last_ok = self.key == key   # nothing committed is known yet: look again soon
        # READ_NONE / READ_ERROR: nothing is forgotten and nothing is final

    def current(self, hwnd: int, title: str) -> Optional[str]:
        """The address to report for this window and title; None when it is not known."""
        if self.key == (hwnd, title):
            return self.url
        return self._app_urls.get(hwnd)

    def app_window(self, hwnd: int) -> Optional[AppWindow]:
        """The window ``hwnd`` as a web app window, when a read found it has no address bar."""
        return self._apps.get(hwnd)


class _UiaWorker:
    """Reads address bars on its own thread (which also owns the UI Automation object)."""

    def __init__(self, reader: "BrowserUrlReader"):
        self._reader = reader
        self._request: Optional[tuple[int, str]] = None
        self._result: Optional[tuple[int, str, str, Optional[str]]] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._done = threading.Event()
        self.busy_since: Optional[float] = None
        self.abandoned = False
        self._thread = threading.Thread(target=self._run, name="uia-reader", daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self.busy_since is not None

    def submit(self, hwnd: int, title: str) -> bool:
        with self._lock:
            if self.busy_since is not None:
                return False
            self._request = (hwnd, title)
            self.busy_since = time.monotonic()
            self._done.clear()
        self._wake.set()
        return True

    def take(self, wait: float = 0.0) -> Optional[tuple[int, str, str, Optional[str]]]:
        if wait > 0:
            self._done.wait(wait)
        with self._lock:
            result, self._result = self._result, None
            return result

    def _run(self) -> None:
        uia = None
        while not self.abandoned:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                request = self._request
                self._request = None
            if request is None:
                continue
            hwnd, title = request
            outcome, value, app = READ_ERROR, None, None
            try:
                if uia is None:
                    uia = self._reader._create_uia()
                read = self._reader._read_omnibox(uia, hwnd)
                outcome, value = read[0], read[1]
                app = read[2] if len(read) > 2 else None
            except Exception as exc:  # noqa: BLE001
                log.debug("omnibox read failed: %s", exc)
            with self._lock:
                if not self.abandoned:
                    self._result = (hwnd, title, outcome, value, app)
                self.busy_since = None
            self._done.set()


class BrowserUrlReader:
    def __init__(self, method: str = "uia", group_by: str = "registrable"):
        self.method = method if _IS_WIN else "title"
        self.group_by = group_by
        self.cache = UrlCache()
        self._worker: Optional[_UiaWorker] = None
        self._abandoned = 0
        self._errors = 0
        self._rests = 0                  # failure streaks so far (the rest after each one is longer)
        self._resting_until = 0.0        # UI Automation is not used before this (monotonic)
        if self.method == "uia":
            try:
                self._init_uia()
            except Exception as exc:  # noqa: BLE001
                log.info("UI Automation unavailable, falling back to window titles: %s", exc)
                self.method = "title"

    def _init_uia(self):
        import comtypes.client  # type: ignore

        # Generate the UI Automation wrapper on first use (comtypes caches it; in the
        # standalone build the cache lives in the user's AppData).  Importing
        # comtypes.gen.UIAutomationClient directly only works if something else
        # happened to generate it before.
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA  # type: ignore

        self._comtypes = __import__("comtypes")
        self._UIA = UIA

    def _create_uia(self):
        """The UI Automation object, made on (and only used by) the reader thread."""
        import comtypes.client  # type: ignore

        # The multi-threaded apartment, as Microsoft recommends for UI Automation clients: the
        # thread has no message loop, and nothing here needs one (no events are subscribed).
        try:
            self._comtypes.CoInitializeEx(getattr(self._comtypes, "COINIT_MULTITHREADED", 0x0))
        except Exception:  # noqa: BLE001 - already initialised on this thread
            try:
                self._comtypes.CoInitialize()
            except Exception:  # noqa: BLE001
                pass
        return comtypes.client.CreateObject(self._UIA.CUIAutomation, interface=self._UIA.IUIAutomation)

    # -- public --------------------------------------------------------------
    def url_and_domain(self, hwnd: int, title: str) -> tuple[Optional[str], Optional[str]]:
        site = self.site(hwnd, title)
        return site.url, site.domain

    def site(self, hwnd: int, title: str) -> SiteInfo:
        """The page in browser window ``hwnd``: its address, domain and full host when known.

        A window without an address bar whose page could not be read is
        ``unknown``; a domain reported with it is a guess (the address the
        browser was started with, or the window title) - good enough for a
        block list, never for an allow list."""
        now = time.monotonic()
        if self.method == "uia" and hwnd and now >= self._resting_until:
            try:
                self._poll_reader(hwnd, title, now)
            except Exception as exc:  # noqa: BLE001
                log.debug("address bar reader failed: %s", exc)
        url = self.cache.current(hwnd, title)
        app = self.cache.app_window(hwnd) if hwnd else None
        domain, host = self._domain_for(url, title), self.host_of(url)
        if app is None:
            return SiteInfo(url=url, domain=domain, host=host)
        if url:
            return SiteInfo(url=url, domain=domain, host=host, app_id=app.app_id)
        if app.start_url:
            domain = domain_from_url(app.start_url, self.group_by) or domain
            host = self.host_of(app.start_url)
        return SiteInfo(url=None, domain=domain, host=host, unknown=True, app_id=app.app_id)

    def _poll_reader(self, hwnd: int, title: str, now: float) -> None:
        worker = self._worker
        if worker is not None:
            self._take(worker.take())
            if worker.busy and now - (worker.busy_since or now) > ABANDON_SECONDS:
                # The browser does not answer.  The call cannot be cancelled; the thread is
                # left to finish on its own and a new one takes over.
                worker.abandoned = True
                self._worker = worker = None
                self._abandoned += 1
                log.warning("reading the browser's address bar did not finish; given up (%d)", self._abandoned)
                if self._abandoned >= MAX_ABANDONED:
                    self._rest(now, "keeps hanging")
                    return
        if not self.cache.due(hwnd, title, now):
            return
        if worker is None:
            self._worker = worker = _UiaWorker(self)
        if worker.submit(hwnd, title):
            self.cache.asked(hwnd, title, now)
            self._take(worker.take(wait=FAST_WAIT_SECONDS))

    def _take(self, result: Optional[tuple]) -> None:
        if result is None:
            return
        hwnd, title, outcome, value = result[:4]
        app = result[4] if len(result) > 4 else None
        if outcome == READ_ERROR:
            self._errors += 1
            if self._errors >= MAX_ERRORS:
                self._rest(time.monotonic(), "keeps failing")
        else:
            self._errors = 0
            if outcome == READ_URL:
                self._rests = 0   # it works again: the next rest is a short one
        self.cache.record(hwnd, title, outcome, value, app)

    def _rest(self, now: float, why: str) -> None:
        """Stop using UI Automation for a while (longer each time); window titles meanwhile."""
        self._rests += 1
        pause = cooldown_seconds(self._rests)
        self._resting_until = now + pause
        self._abandoned = self._errors = 0
        log.warning("UI Automation %s; websites are taken from window titles for the next %d minutes",
                    why, int(pause // 60))

    def _domain_for(self, url: Optional[str], title: str) -> Optional[str]:
        """One rule for first and cached calls: the URL's domain, else whatever the title reveals."""
        domain = domain_from_url(url, self.group_by) if url else None
        return domain if domain else self._domain_from_title(title)

    def domain_only(self, hwnd: int, title: str) -> Optional[str]:
        return self.url_and_domain(hwnd, title)[1]

    @staticmethod
    def host_of(url: Optional[str]) -> Optional[str]:
        """The full host name of an address, lower case ("mail.example.co.uk"); None without one."""
        host = domain_from_url(url, "host") if url else None
        return host.lower() if host else None

    # -- internals -----------------------------------------------------------
    def _domain_from_title(self, title: str) -> Optional[str]:
        if not title:
            return None
        m = _TITLE_DOMAIN_RE.search(title)
        if m:
            return domain_from_url(m.group(1), self.group_by)
        return None

    @staticmethod
    def _is_address_bar(edit) -> bool:
        def prop(prop_id: int) -> str:
            try:
                value = edit.GetCurrentPropertyValue(prop_id)
            except Exception:  # noqa: BLE001
                return ""
            return value if isinstance(value, str) else ""

        return is_address_bar(prop(_UIA_AUTOMATIONID_PROPERTY), prop(_UIA_CLASSNAME_PROPERTY), prop(_UIA_NAME_PROPERTY))

    def _read_omnibox(self, uia, hwnd: int) -> tuple:
        """(outcome, address[, app window]) for the browser window ``hwnd``.  Runs on the reader thread."""
        element = uia.ElementFromHandle(hwnd)
        if not element:
            return READ_NONE, None
        # Only the browser's own interface is looked through, never the page: a search of the
        # whole window would walk into the web content and make the browser build its
        # accessibility tree for it (slow, and it stays on).  A window without an address bar
        # (a web app window, a full-screen video) is read from its page's Document element.
        bar = self._toolbar_search(uia, element)
        if not bar:
            return self._read_app_window(uia, element, hwnd)
        try:
            focused = bool(bar.GetCurrentPropertyValue(_UIA_HASKEYBOARDFOCUS_PROPERTY))
        except Exception:  # noqa: BLE001
            focused = False
        if focused:
            return READ_TYPING, None   # what is in the bar now is typed text, not a visited page
        value = bar.GetCurrentPropertyValue(_UIA_VALUE_PROPERTY)
        value = value.strip() if isinstance(value, str) else ""
        return (READ_URL, value) if looks_like_address(value) else (READ_NONE, None)

    def _read_app_window(self, uia, element, hwnd: int) -> tuple:
        """A window without an address bar: the address of its page, read from the page's
        Document element (reader thread).

        Chromium shows the Document among the window's elements only once its
        accessibility support is on; asking the page's own window
        (``Chrome_RenderWidgetHostHWND``) for its element switches it on, and the
        Document is looked for once more after a moment.  Nothing inside the
        page is ever looked at: the Document's own value is the address."""
        from .win_activity import child_window

        documents = self._document_search(uia, element)
        render_widget = child_window(hwnd, RENDER_WIDGET_CLASS) if not documents else 0
        if not documents and render_widget:
            try:
                page = uia.ElementFromHandle(render_widget)
                if page:
                    page.CurrentName   # noqa: B018 - the question is what wakes the browser up
            except Exception as exc:  # noqa: BLE001
                log.debug("page window not available: %s", exc)
            time.sleep(DOCUMENT_SETTLE_SECONDS)
            documents = self._document_search(uia, element)
        if not documents and not render_widget:
            return READ_NONE, None   # no web page in this window (a dialog, the browser's own settings window)
        app = self._app_identity(hwnd)
        url = self._document_url(documents, hwnd)
        return (READ_URL, url, app) if url else (READ_APP, None, app)

    @staticmethod
    def _document_url(documents: list, hwnd: int) -> Optional[str]:
        """The address of the page in front among the Documents found (reader thread)."""
        names = [name for _element, name in documents]
        title = ""
        if len(documents) > 1:
            from .win_activity import window_title

            title = window_title(hwnd)
        index = pick_document(names, title)
        if index is None:
            return None
        document = documents[index][0]
        for prop_id in (_UIA_VALUE_PROPERTY, _UIA_LEGACY_VALUE_PROPERTY):
            try:
                url = document_address(document.GetCurrentPropertyValue(prop_id))
            except Exception:  # noqa: BLE001
                url = None
            if url:
                return url
        return None

    @staticmethod
    def _app_identity(hwnd: int) -> AppWindow:
        """Who the window without an address bar belongs to: its AppUserModelID and the
        browser's --app / --app-id arguments (reader thread)."""
        from .win_activity import window_app_user_model_id, window_process_id

        aumid = window_app_user_model_id(hwnd)
        args: list = []
        pid = window_process_id(hwnd)
        if pid:
            try:
                import psutil  # type: ignore

                args = psutil.Process(pid).cmdline()
            except Exception:  # noqa: BLE001 - gone, or not ours to read
                args = []
        return app_window_identity(aumid, args)

    @staticmethod
    def _document_search(uia, root) -> list:
        """[(Document element, its name)] among the browser's own containers (reader thread).

        Bounded like the tool bar search; a Document is collected, never opened."""
        found: list = []
        try:
            request = uia.CreateCacheRequest()
            for prop_id in (_UIA_CONTROLTYPE_PROPERTY, _UIA_NAME_PROPERTY):
                request.AddProperty(prop_id)
            everything = uia.CreateTrueCondition()
            queue = [root]
            expanded = 0
            while queue and expanded < MAX_EXPANDED and len(found) < MAX_DOCUMENTS:
                node = queue.pop(0)
                expanded += 1
                children = node.FindAllBuildCache(2, everything, request)   # TreeScope_Children
                if not children:
                    continue
                for i in range(min(children.Length, MAX_CHILDREN)):
                    child = children.GetElement(i)
                    try:
                        ctype = child.GetCachedPropertyValue(_UIA_CONTROLTYPE_PROPERTY)
                    except Exception:  # noqa: BLE001
                        ctype = None
                    step = document_step(ctype)
                    if step == "document":
                        try:
                            name = child.GetCachedPropertyValue(_UIA_NAME_PROPERTY)
                        except Exception:  # noqa: BLE001
                            name = ""
                        found.append((child, name if isinstance(name, str) else ""))
                        if len(found) >= MAX_DOCUMENTS:
                            break
                    elif step == "expand":
                        queue.append(child)
        except Exception as exc:  # noqa: BLE001
            log.debug("document search failed: %s", exc)
        return found

    @staticmethod
    def _toolbar_search(uia, root):
        """The address bar among the browser's own controls, or None (reader thread)."""
        try:
            request = uia.CreateCacheRequest()
            for prop_id in (_UIA_CONTROLTYPE_PROPERTY, _UIA_AUTOMATIONID_PROPERTY, _UIA_CLASSNAME_PROPERTY, _UIA_NAME_PROPERTY):
                request.AddProperty(prop_id)
            everything = uia.CreateTrueCondition()
            try:
                # the address bar by what it is called (verified on Chrome and Firefox; Edge and the
                # other Chromium browsers share Chrome's class), whatever its control type
                known = uia.CreateOrCondition(
                    uia.CreatePropertyCondition(_UIA_CLASSNAME_PROPERTY, _OMNIBOX_CLASS),
                    uia.CreatePropertyCondition(_UIA_AUTOMATIONID_PROPERTY, _FIREFOX_URLBAR_ID))
            except Exception:  # noqa: BLE001
                known = None
            queue = [root]
            expanded = 0
            while queue and expanded < MAX_EXPANDED:
                node = queue.pop(0)
                expanded += 1
                children = node.FindAllBuildCache(2, everything, request)   # TreeScope_Children
                if not children:
                    continue
                for i in range(min(children.Length, MAX_CHILDREN)):
                    child = children.GetElement(i)

                    def cached(prop_id: int, element=child) -> Any:
                        try:
                            return element.GetCachedPropertyValue(prop_id)
                        except Exception:  # noqa: BLE001
                            return None

                    def text(prop_id: int) -> str:
                        value = cached(prop_id)
                        return value if isinstance(value, str) else ""

                    step = toolbar_step(cached(_UIA_CONTROLTYPE_PROPERTY), text(_UIA_AUTOMATIONID_PROPERTY),
                                        text(_UIA_CLASSNAME_PROPERTY), text(_UIA_NAME_PROPERTY))
                    if step == "match":
                        return child
                    if known is not None and cached(_UIA_CONTROLTYPE_PROPERTY) == _UIA_TOOLBAR:
                        # a tool bar holds no web content: here a full search is safe, and quick
                        try:
                            bar = child.FindFirst(4, known)   # TreeScope_Descendants, inside the tool bar only
                        except Exception:  # noqa: BLE001
                            bar = None
                        if bar:
                            return bar
                    if step == "expand":
                        queue.append(child)
        except Exception as exc:  # noqa: BLE001
            log.debug("toolbar search failed: %s", exc)
        return None
