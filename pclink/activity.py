"""Classify what a user is doing from the agent's foreground report."""
from __future__ import annotations

import re
from typing import Optional

from .config import ActivityConfig
from .models import Activity, AgentReport, ForegroundInfo


def _norm_path(path: str) -> str:
    return path.replace("/", "\\").lower()


class ActivityClassifier:
    def __init__(self, cfg: ActivityConfig):
        self.cfg = cfg
        self.browsers = {b.lower() for b in cfg.browsers}
        self.games = {g.lower() for g in cfg.games}
        self.not_games = {g.lower() for g in cfg.not_games}
        self.launchers = {l.lower() for l in cfg.launchers}
        self.non_game_fullscreen = {n.lower() for n in cfg.non_game_fullscreen} | self.browsers
        self.game_paths = [_norm_path(p) for p in cfg.game_paths if p]
        self.title_patterns = [re.compile(p, re.I) for p in cfg.game_title_patterns if p]
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

    def site_category(self, domain: Optional[str]) -> str:
        if not domain:
            return "browsing"
        d = domain.lower()
        if d in self.site_categories:
            return self.site_categories[d]
        for pattern, category in self.site_categories.items():
            if d.endswith("." + pattern):
                return category
        return "browsing"

    # -- main entry -----------------------------------------------------------
    def classify(self, report: Optional[AgentReport], locked: Optional[bool] = None) -> Activity:
        if report is None:
            return Activity(category="none")
        if locked or report.locked:
            return Activity(category="locked")
        fg = report.foreground
        idle = report.idle_seconds >= self.cfg.idle_seconds
        if idle and not (self.cfg.audio_keeps_active and report.audio_active):
            return Activity(category="idle", app=fg.name.lower() if fg else None, title=fg.title if fg else None)
        if fg is None or not fg.name:
            return Activity(category="idle")
        name = fg.name.lower()
        if self.is_browser(name):
            domain = report.domain
            return Activity(
                category=self.site_category(domain), app=name, exe=fg.exe or None,
                title=fg.title or None, domain=domain, url=report.url,
            )
        if self.is_game(fg):
            return Activity(category="gaming", app=name, exe=fg.exe or None, title=fg.title or None)
        return Activity(category="app", app=name, exe=fg.exe or None, title=fg.title or None)
