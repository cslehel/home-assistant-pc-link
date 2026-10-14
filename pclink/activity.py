"""Classify what a user is doing from the agent's foreground report."""
from __future__ import annotations

import logging
import re
from typing import Optional

from .config import ActivityConfig
from .models import Activity, AgentReport, ForegroundInfo
from .util import domain_from_url


log = logging.getLogger(__name__)


def _norm_path(path: str) -> str:
    return path.replace("/", "\\").lower()


class ActivityClassifier:
    def __init__(self, cfg: ActivityConfig):
        self.cfg = cfg
        self.browsers = {b.lower() for b in cfg.browsers}
        self.games = {g.lower() for g in cfg.games}
        self.not_games = {g.lower() for g in cfg.not_games}
        self.launchers = {l.lower() for l in cfg.launchers}
        # a film full screen in a media player (or a browser) is not a game
        self.non_game_fullscreen = ({n.lower() for n in cfg.non_game_fullscreen} | self.browsers
                                    | {m.lower() for m in cfg.media_apps})
        self.game_paths = [_norm_path(p) for p in cfg.game_paths if p]
        self.title_patterns = []
        for pattern in cfg.game_title_patterns:
            if not pattern:
                continue
            try:
                self.title_patterns.append(re.compile(str(pattern), re.I))
            except re.error as exc:  # Config.load rejects these; never let one stop the service
                log.error("activity.game_title_patterns: ignoring the invalid pattern %r (%s)", pattern, exc)
        self.site_categories = dict(cfg.site_categories)

    # -- helpers ------------------------------------------------------------
    def is_browser(self, name: str) -> bool:
        return name.lower() in self.browsers

    def is_game(self, fg: ForegroundInfo) -> bool:
        name = (fg.name or "").lower()
        if not name or name in self.not_games or name in self.launchers or name in self.browsers:
            return False
        if name in self.games:
            return True
        exe = _norm_path(fg.exe or "")
        if exe and any(p in exe for p in self.game_paths):
            return True
        if fg.title and any(p.search(fg.title) for p in self.title_patterns):
            return True
        if self.cfg.fullscreen_heuristic and fg.fullscreen and name not in self.non_game_fullscreen:
            return True
        return False

    def site_category(self, domain: Optional[str], url: Optional[str] = None) -> str:
        """The category of a site.  The page's full host (from ``url``, when the agent
        could read the address) is looked up before the reported domain, so a category
        for ``docs.google.com`` works although the domain is grouped as ``google.com``."""
        names = []
        host = domain_from_url(url, "host") if url else None
        if host:
            names.append(host)
        if domain and domain.lower() not in names:
            names.append(domain.lower())
        for d in names:
            if d in self.site_categories:
                return self.site_categories[d]
            # the longest matching suffix wins, whatever the order in the configuration
            for pattern, category in sorted(self.site_categories.items(), key=lambda kv: -len(kv[0])):
                if d.endswith("." + pattern):
                    return category
        return "browsing"

    # -- main entry -----------------------------------------------------------
    def _passive_use(self, report: AgentReport) -> bool:
        """No input, but the seat is clearly in use (a video or full-screen game
        is running): the same signals ``presence.evaluate`` calls "passive"."""
        if not self.cfg.media_keeps_active:
            return False
        if report.media is not None and report.media.is_playing:
            return True
        return bool(report.fullscreen_d3d or report.presentation_mode)

    def classify(self, report: Optional[AgentReport], locked: Optional[bool] = None,
                 presence: Optional[str] = None) -> Activity:
        """``presence`` is the session's presence verdict when the caller has
        one: a "passive" seat (watching a muted video) is still using the app,
        so it is not reported as idle and per-app / per-site rules keep applying."""
        if report is None:
            return Activity(category="none")
        if locked or report.locked:
            return Activity(category="locked")
        fg = report.foreground
        idle = report.idle_seconds >= self.cfg.idle_seconds
        if idle and (presence == "passive" or (presence is None and self._passive_use(report))):
            idle = False
        if idle and not (self.cfg.audio_keeps_active and report.audio_active):
            return Activity(category="idle", app=fg.name.lower() if fg else None, title=fg.title if fg else None)
        if fg is None or not fg.name:
            return Activity(category="idle")
        name = fg.name.lower()
        if self.is_browser(name):
            domain = report.domain
            # a page whose address could not be read is still a page: its time counts as browsing
            return Activity(
                category=self.site_category(domain, report.url), app=name, exe=fg.exe or None,
                title=fg.title or None, domain=domain, url=report.url,
                site_unknown=bool(report.site_unknown), app_id=report.app_id if report.site_unknown else None,
            )
        if self.is_game(fg):
            return Activity(category="gaming", app=name, exe=fg.exe or None, title=fg.title or None)
        return Activity(category="app", app=name, exe=fg.exe or None, title=fg.title or None)
