"""SQLite persistence for usage statistics and HA-controlled settings.

Usage counters are accumulated in memory and flushed periodically so that the
service never hammers the disk (a single UPSERT batch every ~30 s).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    user TEXT NOT NULL, day TEXT NOT NULL,
    active REAL NOT NULL DEFAULT 0, gaming REAL NOT NULL DEFAULT 0, browsing REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user, day)
);
CREATE TABLE IF NOT EXISTS app_usage (
    user TEXT NOT NULL, day TEXT NOT NULL, app TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user, day, app)
);
CREATE TABLE IF NOT EXISTS site_usage (
    user TEXT NOT NULL, day TEXT NOT NULL, domain TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user, day, domain)
);
CREATE TABLE IF NOT EXISTS settings (
    user TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (user, key)
);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL, user TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


@dataclass
class UsageTotals:
    active: float = 0.0
    gaming: float = 0.0
    browsing: float = 0.0


class Store:
    GLOBAL = "_global"

    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        # memory caches ------------------------------------------------------
        self._usage: dict[tuple[str, str], UsageTotals] = {}
        self._apps: dict[tuple[str, str], dict[str, float]] = {}
        self._sites: dict[tuple[str, str], dict[str, float]] = {}
        self._dirty_usage: set[tuple[str, str]] = set()
        self._dirty_apps: set[tuple[str, str]] = set()
        self._dirty_sites: set[tuple[str, str]] = set()
        self._settings: dict[tuple[str, str], Any] = {}
        self._load_settings()

    # -- usage --------------------------------------------------------------
    def usage(self, user: str, day: date) -> UsageTotals:
        key = (user.lower(), day.isoformat())
        with self._lock:
            totals = self._usage.get(key)
            if totals is None:
                row = self._db.execute(
                    "SELECT active, gaming, browsing FROM usage WHERE user=? AND day=?", key
                ).fetchone()
                totals = UsageTotals(*row) if row else UsageTotals()
                self._usage[key] = totals
            return totals

    def add_usage(self, user: str, day: date, active: float = 0.0, gaming: float = 0.0, browsing: float = 0.0) -> None:
        if active <= 0 and gaming <= 0 and browsing <= 0:
            return
        with self._lock:
            totals = self.usage(user, day)
            totals.active += max(0.0, active)
            totals.gaming += max(0.0, gaming)
            totals.browsing += max(0.0, browsing)
            self._dirty_usage.add((user.lower(), day.isoformat()))

    def _bucket(self, cache, table, column, user: str, day: date) -> dict[str, float]:
        key = (user.lower(), day.isoformat())
        bucket = cache.get(key)
        if bucket is None:
            rows = self._db.execute(
                f"SELECT {column}, seconds FROM {table} WHERE user=? AND day=?", key
            ).fetchall()
            bucket = {name: float(secs) for name, secs in rows}
            cache[key] = bucket
        return bucket

    def add_app(self, user: str, day: date, app: str, seconds: float) -> None:
        if seconds <= 0 or not app:
            return
        with self._lock:
            bucket = self._bucket(self._apps, "app_usage", "app", user, day)
            bucket[app] = bucket.get(app, 0.0) + seconds
            self._dirty_apps.add((user.lower(), day.isoformat()))

    def add_site(self, user: str, day: date, domain: str, seconds: float) -> None:
        if seconds <= 0 or not domain:
            return
        with self._lock:
            bucket = self._bucket(self._sites, "site_usage", "domain", user, day)
            bucket[domain] = bucket.get(domain, 0.0) + seconds
            self._dirty_sites.add((user.lower(), day.isoformat()))

    def top_apps(self, user: str, day: date, n: int = 10) -> list[tuple[str, float]]:
        with self._lock:
            bucket = self._bucket(self._apps, "app_usage", "app", user, day)
            return sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def top_sites(self, user: str, day: date, n: int = 10) -> list[tuple[str, float]]:
        with self._lock:
            bucket = self._bucket(self._sites, "site_usage", "domain", user, day)
            return sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def history(self, user: str, days: int = 7, today: Optional[date] = None) -> list[dict[str, Any]]:
        today = today or date.today()
        out = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            totals = self.usage(user, day)
            out.append(
                {
                    "date": day.isoformat(),
                    "active_minutes": round(totals.active / 60, 1),
                    "gaming_minutes": round(totals.gaming / 60, 1),
                    "browsing_minutes": round(totals.browsing / 60, 1),
                }
            )
        return out

    # -- settings -----------------------------------------------------------
    def _load_settings(self) -> None:
        with self._lock:
            for user, key, value in self._db.execute("SELECT user, key, value FROM settings"):
                try:
                    self._settings[(user, key)] = json.loads(value) if value is not None else None
                except json.JSONDecodeError:
                    self._settings[(user, key)] = value

    def get_setting(self, user: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._settings.get((user.lower(), key), default)

    def set_setting(self, user: str, key: str, value: Any) -> None:
        with self._lock:
            self._settings[(user.lower(), key)] = value
            self._db.execute(
                "INSERT INTO settings(user, key, value) VALUES(?,?,?) "
                "ON CONFLICT(user, key) DO UPDATE SET value=excluded.value",
                (user.lower(), key, json.dumps(value)),
            )

    def delete_setting(self, user: str, key: str) -> None:
        with self._lock:
            self._settings.pop((user.lower(), key), None)
            self._db.execute("DELETE FROM settings WHERE user=? AND key=?", (user.lower(), key))

    def settings_for(self, user: str) -> dict[str, Any]:
        with self._lock:
            return {k: v for (u, k), v in self._settings.items() if u == user.lower()}

    # -- events -------------------------------------------------------------
    def log_event(self, user: Optional[str], kind: str, detail: str = "") -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO events(ts, user, kind, detail) VALUES(?,?,?,?)",
                    (time.time(), (user or "").lower() or None, kind, detail[:500]),
                )
            except sqlite3.Error as exc:  # never let logging break the service
                log.debug("event insert failed: %s", exc)

    def recent_events(self, n: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, user, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (n,)
            ).fetchall()
        return [{"ts": ts, "user": u, "kind": k, "detail": d} for ts, u, k, d in rows]

    # -- maintenance --------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            if not (self._dirty_usage or self._dirty_apps or self._dirty_sites):
                return
            try:
                self._db.execute("BEGIN")
                for user, day in self._dirty_usage:
                    t = self._usage[(user, day)]
                    self._db.execute(
                        "INSERT INTO usage(user, day, active, gaming, browsing) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user, day) DO UPDATE SET active=excluded.active, gaming=excluded.gaming, browsing=excluded.browsing",
                        (user, day, t.active, t.gaming, t.browsing),
                    )
                for user, day in self._dirty_apps:
                    for app, secs in self._apps[(user, day)].items():
                        self._db.execute(
                            "INSERT INTO app_usage(user, day, app, seconds) VALUES(?,?,?,?) "
                            "ON CONFLICT(user, day, app) DO UPDATE SET seconds=excluded.seconds",
                            (user, day, app, secs),
                        )
                for user, day in self._dirty_sites:
                    for domain, secs in self._sites[(user, day)].items():
                        self._db.execute(
                            "INSERT INTO site_usage(user, day, domain, seconds) VALUES(?,?,?,?) "
                            "ON CONFLICT(user, day, domain) DO UPDATE SET seconds=excluded.seconds",
                            (user, day, domain, secs),
                        )
                self._db.execute("COMMIT")
            except sqlite3.Error as exc:
                log.error("store flush failed: %s", exc)
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return
            self._dirty_usage.clear()
            self._dirty_apps.clear()
            self._dirty_sites.clear()

    def purge(self, retention_days: int, today: Optional[date] = None) -> None:
        if retention_days <= 0:
            return
        cutoff = ((today or date.today()) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            try:
                for table in ("usage", "app_usage", "site_usage"):
                    self._db.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
                self._db.execute("DELETE FROM events WHERE ts < ?", (time.time() - retention_days * 86400,))
                for key in [k for k in self._settings if k[1].startswith(("bonus:", "limit:")) and k[1][k[1].index(":") + 1 :] < cutoff]:
                    self.delete_setting(*key)
                # drop stale in-memory day buckets
                for cache in (self._usage, self._apps, self._sites):
                    for key in [k for k in cache if k[1] < cutoff]:
                        cache.pop(key, None)
            except sqlite3.Error as exc:
                log.warning("store purge failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            self.flush()
            self._db.close()
