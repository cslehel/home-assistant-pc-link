"""SQLite persistence for usage statistics and HA-controlled settings.

Usage counters are accumulated in memory and flushed periodically so that the
service never hammers the disk (a single UPSERT batch every ~30 s).
"""
from __future__ import annotations

import heapq
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
CREATE TABLE IF NOT EXISTS rule_usage (
    user TEXT NOT NULL, day TEXT NOT NULL, rule TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user, day, rule)
);
CREATE TABLE IF NOT EXISTS settings (
    user TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (user, key)
);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL, user TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS usage_hourly (
    user TEXT NOT NULL, hour TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user, hour, kind, name)
);
"""

HOURLY_KEEP_DAYS = 14   # per-hour detail (the "last N hours" report) is kept this long
# Day buckets are kept in memory this many days back (this week and last week's summary
# need 14); older ones are read from the database when asked for.
CACHE_DAYS = 16

# The schema's version, kept in the database as PRAGMA user_version (0 = a database from
# before versions were kept; its tables are the same as version 1's).  To change the
# schema: raise the number and add the step that takes a database there to _MIGRATIONS.
SCHEMA_VERSION = 1
_MIGRATIONS: dict[int, Any] = {}   # version -> callable(connection), run inside a transaction


def hour_key(when: datetime) -> str:
    """The local hour a moment falls in, as stored in ``usage_hourly`` (sorts chronologically)."""
    return when.strftime("%Y-%m-%dT%H")


@dataclass
class UsageTotals:
    active: float = 0.0
    gaming: float = 0.0
    browsing: float = 0.0


_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dated_key_day(key: str) -> Optional[str]:
    """Return the ISO day a per-day setting key ends with, else None."""
    if ":" not in key:
        return None
    tail = key.rsplit(":", 1)[1]
    return tail if _ISO_DAY_RE.match(tail) else None


class Store:
    GLOBAL = "_global"

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.recovered_from: Optional[str] = None   # where a corrupt database was moved to, if it was
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._db = self._open(path)
        except sqlite3.DatabaseError as exc:
            # "file is not a database" / "database disk image is malformed".  A database
            # that is merely locked or cannot be opened (OperationalError) is not ours to
            # throw away.
            if path == ":memory:" or isinstance(exc, sqlite3.OperationalError):
                raise
            self.recovered_from = self._move_aside(path)
            log.error("the database %s is corrupt (%s); it was moved to %s and a new, empty one is used - "
                      "usage statistics and settings made from Home Assistant start afresh",
                      path, exc, self.recovered_from)
            self._db = self._open(path)
        # memory caches ------------------------------------------------------
        self._usage: dict[tuple[str, str], UsageTotals] = {}
        self._apps: dict[tuple[str, str], dict[str, float]] = {}
        self._sites: dict[tuple[str, str], dict[str, float]] = {}
        self._rules: dict[tuple[str, str], dict[str, float]] = {}
        self._dirty_usage: set[tuple[str, str]] = set()
        self._dirty_apps: set[tuple[str, str]] = set()
        self._dirty_sites: set[tuple[str, str]] = set()
        self._dirty_rules: set[tuple[str, str]] = set()
        self._settings: dict[tuple[str, str], Any] = {}
        self._hourly: dict[tuple[str, str, str, str], float] = {}   # seconds not written yet
        self._load_settings()

    # -- opening ------------------------------------------------------------
    @classmethod
    def _open(cls, path: str) -> sqlite3.Connection:
        """Open (or create) the database, check it and bring its schema up to date."""
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            if path != ":memory:":
                verdict = db.execute("PRAGMA quick_check(1)").fetchone()
                if not verdict or str(verdict[0]).lower() != "ok":
                    raise sqlite3.DatabaseError(f"integrity check failed: {verdict[0] if verdict else 'no result'}")
            db.executescript(_SCHEMA)
            cls._migrate(db)
            db.execute("SELECT user, key, value FROM settings LIMIT 1").fetchall()
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Bring an older database to ``SCHEMA_VERSION`` (``PRAGMA user_version``).

        ``_SCHEMA`` has already created whatever tables were missing; a step in
        ``_MIGRATIONS`` is for what ``CREATE TABLE IF NOT EXISTS`` cannot do (a new
        column, converted data).  A database written by a *newer* version is used as
        it is: unknown tables and columns are simply not touched."""
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            if version > SCHEMA_VERSION:
                log.warning("the database was written by a newer version (schema %d, this version knows %d)",
                            version, SCHEMA_VERSION)
            return
        for target in range(version + 1, SCHEMA_VERSION + 1):
            step = _MIGRATIONS.get(target)
            db.execute("BEGIN")
            try:
                if step is not None:
                    step(db)
                db.execute(f"PRAGMA user_version = {int(target)}")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _move_aside(path: str) -> str:
        """Rename a corrupt database (and its WAL files) to ``<path>.corrupt-<timestamp>``."""
        target = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        n = 1
        while os.path.exists(target):
            n += 1
            target = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        os.replace(path, target)
        for suffix in ("-wal", "-shm"):
            try:
                os.replace(path + suffix, target + suffix)
            except OSError:
                pass
        return target

    # -- usage --------------------------------------------------------------
    @staticmethod
    def _cached_day(day_iso: str) -> bool:
        """Is a day recent enough to keep in memory once read?  Older days (the history
        sensor, an old week's summary) are read from the database each time instead of
        filling memory for the whole retention period."""
        return day_iso >= (date.today() - timedelta(days=CACHE_DAYS)).isoformat()

    def usage(self, user: str, day: date) -> UsageTotals:
        """The day's totals.  (A copy for an old day that is not in memory: change them with ``add_usage``.)"""
        return self._usage_totals(user, day, keep=False)

    def _usage_totals(self, user: str, day: date, keep: bool) -> UsageTotals:
        key = (user.lower(), day.isoformat())
        with self._lock:
            totals = self._usage.get(key)
            if totals is None:
                row = self._db.execute(
                    "SELECT active, gaming, browsing FROM usage WHERE user=? AND day=?", key
                ).fetchone()
                totals = UsageTotals(*row) if row else UsageTotals()
                if keep or self._cached_day(key[1]):
                    self._usage[key] = totals
            return totals

    def add_usage(self, user: str, day: date, active: float = 0.0, gaming: float = 0.0, browsing: float = 0.0) -> None:
        if active <= 0 and gaming <= 0 and browsing <= 0:
            return
        with self._lock:
            totals = self._usage_totals(user, day, keep=True)
            totals.active += max(0.0, active)
            totals.gaming += max(0.0, gaming)
            totals.browsing += max(0.0, browsing)
            self._dirty_usage.add((user.lower(), day.isoformat()))

    def _bucket(self, cache, table, column, user: str, day: date, keep: bool = False) -> dict[str, float]:
        """A day's per-name seconds.  ``keep``: the caller changes it, so it must be the
        cached one (an old day that is only read is not cached, see ``_cached_day``)."""
        key = (user.lower(), day.isoformat())
        bucket = cache.get(key)
        if bucket is None:
            rows = self._db.execute(
                f"SELECT {column}, seconds FROM {table} WHERE user=? AND day=?", key
            ).fetchall()
            bucket = {name: float(secs) for name, secs in rows}
            if keep or self._cached_day(key[1]):
                cache[key] = bucket
        return bucket

    def add_app(self, user: str, day: date, app: str, seconds: float) -> None:
        if seconds <= 0 or not app:
            return
        with self._lock:
            bucket = self._bucket(self._apps, "app_usage", "app", user, day, keep=True)
            bucket[app] = bucket.get(app, 0.0) + seconds
            self._dirty_apps.add((user.lower(), day.isoformat()))

    def add_site(self, user: str, day: date, domain: str, seconds: float) -> None:
        if seconds <= 0 or not domain:
            return
        with self._lock:
            bucket = self._bucket(self._sites, "site_usage", "domain", user, day, keep=True)
            bucket[domain] = bucket.get(domain, 0.0) + seconds
            self._dirty_sites.add((user.lower(), day.isoformat()))

    def add_hourly(self, user: str, when: datetime, kind: str, name: str, seconds: float) -> None:
        """Per-hour detail behind the "last N hours" report (``kind``: app | game | site)."""
        if seconds <= 0 or not name:
            return
        key = (user.lower(), hour_key(when), kind, name)
        with self._lock:
            self._hourly[key] = self._hourly.get(key, 0.0) + seconds

    def recent_usage(self, user: str, since: datetime) -> dict[str, dict[str, float]]:
        """Seconds per name, by kind, from the hour ``since`` falls in until now."""
        user, first = user.lower(), hour_key(since)
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, name, SUM(seconds) FROM usage_hourly WHERE user=? AND hour>=? GROUP BY kind, name",
                (user, first)).fetchall()
            for kind, name, secs in rows:
                out.setdefault(kind, {})[name] = float(secs or 0.0)
            for (u, hour, kind, name), secs in self._hourly.items():
                if u == user and hour >= first:
                    bucket = out.setdefault(kind, {})
                    bucket[name] = bucket.get(name, 0.0) + secs
        return out

    def top_apps(self, user: str, day: date, n: int = 10) -> list[tuple[str, float]]:
        with self._lock:
            bucket = self._bucket(self._apps, "app_usage", "app", user, day)
            # (the day's bucket is in memory; picking n of it is cheaper than sorting it all)
            return heapq.nlargest(n, bucket.items(), key=lambda kv: kv[1])

    def top_sites(self, user: str, day: date, n: int = 10) -> list[tuple[str, float]]:
        with self._lock:
            bucket = self._bucket(self._sites, "site_usage", "domain", user, day)
            return heapq.nlargest(n, bucket.items(), key=lambda kv: kv[1])

    # -- per-rule usage (app / site limits) ------------------------------------
    def add_rule(self, user: str, day: date, rule: str, seconds: float) -> None:
        if seconds <= 0 or not rule:
            return
        with self._lock:
            bucket = self._bucket(self._rules, "rule_usage", "rule", user, day, keep=True)
            bucket[rule] = bucket.get(rule, 0.0) + seconds
            self._dirty_rules.add((user.lower(), day.isoformat()))

    def rule_seconds(self, user: str, day: date, rule: str) -> float:
        with self._lock:
            return self._bucket(self._rules, "rule_usage", "rule", user, day).get(rule, 0.0)

    def rule_seconds_range(self, user: str, start: date, end: date, rule: str) -> float:
        """Seconds counted for ``rule`` from ``start`` to ``end`` (both inclusive)."""
        total = 0.0
        day = start
        while day <= end:
            total += self.rule_seconds(user, day, rule)
            day += timedelta(days=1)
        return total

    def rule_usage(self, user: str, day: date) -> dict[str, float]:
        with self._lock:
            return dict(self._bucket(self._rules, "rule_usage", "rule", user, day))

    # -- ranges (weekly budgets) ----------------------------------------------------
    def usage_range(self, user: str, start: date, end: date) -> UsageTotals:
        """Sum of daily totals from ``start`` to ``end`` (both inclusive)."""
        total = UsageTotals()
        day = start
        while day <= end:
            t = self.usage(user, day)
            total.active += t.active
            total.gaming += t.gaming
            total.browsing += t.browsing
            day += timedelta(days=1)
        return total

    def summary_range(self, user: str, start: date, end: date, top: int = 5) -> dict[str, Any]:
        """What ``user`` did from ``start`` to ``end`` (both inclusive), for the weekly summary:
        ``{"days": {iso day: active seconds}, "active_seconds": total, "apps": [...],
        "sites": [...], "games": [...]}`` - each list the ``top`` names by time, as
        ``{"name", "seconds"}``.

        Games and other programs are told apart by how the orchestrator books them
        (``game:<name>`` / ``app:<name>`` in the per-app counters); a name without either
        prefix counts as a program."""
        days: dict[str, int] = {}
        apps: dict[str, float] = {}
        games: dict[str, float] = {}
        sites: dict[str, float] = {}
        day = start
        with self._lock:
            while day <= end:
                days[day.isoformat()] = int(round(self.usage(user, day).active))
                for name, secs in self._bucket(self._apps, "app_usage", "app", user, day).items():
                    kind, _, plain = name.partition(":")
                    target, label = (games, plain) if kind == "game" and plain else \
                        (apps, plain if kind == "app" and plain else name)
                    target[label] = target.get(label, 0.0) + secs
                for name, secs in self._bucket(self._sites, "site_usage", "domain", user, day).items():
                    sites[name] = sites.get(name, 0.0) + secs
                day += timedelta(days=1)

        def ranked(totals: dict[str, float]) -> list[dict[str, Any]]:
            best = heapq.nlargest(max(0, int(top)), totals.items(), key=lambda kv: kv[1])
            return [{"name": name, "seconds": int(round(secs))} for name, secs in best]

        return {"days": days, "active_seconds": sum(days.values()),
                "apps": ranked(apps), "sites": ranked(sites), "games": ranked(games)}

    def count_events(self, user: Optional[str], kind: str, since_ts: float, until_ts: float) -> int:
        """How many events of ``kind`` were logged from ``since_ts`` up to (not including)
        ``until_ts``, for ``user`` or - with None - for everybody."""
        sql = "SELECT COUNT(*) FROM events WHERE kind=? AND ts>=? AND ts<?"
        params: list[Any] = [kind, float(since_ts), float(until_ts)]
        if user:
            sql += " AND user=?"
            params.append(user.lower())
        with self._lock:
            try:
                return int(self._db.execute(sql, params).fetchone()[0])
            except sqlite3.Error as exc:
                log.debug("event count failed: %s", exc)
                return 0

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
        """Persist a setting.  The database is written first, so a failed write
        (which is logged and re-raised) never leaves memory ahead of the disk."""
        encoded = json.dumps(value)
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO settings(user, key, value) VALUES(?,?,?) "
                    "ON CONFLICT(user, key) DO UPDATE SET value=excluded.value",
                    (user.lower(), key, encoded),
                )
            except sqlite3.Error as exc:
                log.error("could not save setting %s for %s: %s", key, user, exc)
                raise
            self._settings[(user.lower(), key)] = value

    def delete_setting(self, user: str, key: str) -> None:
        with self._lock:
            try:
                self._db.execute("DELETE FROM settings WHERE user=? AND key=?", (user.lower(), key))
            except sqlite3.Error as exc:
                log.error("could not delete setting %s for %s: %s", key, user, exc)
                raise
            self._settings.pop((user.lower(), key), None)

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

    def recent_events(self, n: int = 20, user: Optional[str] = None, kind: Optional[str] = None,
                      since: Optional[float] = None) -> list[dict[str, Any]]:
        sql = "SELECT ts, user, kind, detail FROM events"
        where, params = [], []
        if user:
            where.append("user=?")
            params.append(user.lower())
        if kind:
            where.append("kind=?")
            params.append(kind)
        if since is not None:
            where.append("ts>=?")
            params.append(float(since))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(n))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [{"ts": ts, "user": u, "kind": k, "detail": d} for ts, u, k, d in rows]

    # -- maintenance --------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            if not (self._dirty_usage or self._dirty_apps or self._dirty_sites or self._dirty_rules or self._hourly):
                return
            try:
                self._db.execute("BEGIN")
                for (user, hour, kind, name), secs in self._hourly.items():
                    self._db.execute(
                        "INSERT INTO usage_hourly(user, hour, kind, name, seconds) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user, hour, kind, name) DO UPDATE SET seconds=seconds+excluded.seconds",
                        (user, hour, kind, name, secs),
                    )
                for user, day in self._dirty_usage:
                    t = self._usage.get((user, day))
                    if t is None:  # purged in the meantime (the clock was moved a long way)
                        continue
                    self._db.execute(
                        "INSERT INTO usage(user, day, active, gaming, browsing) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user, day) DO UPDATE SET active=excluded.active, gaming=excluded.gaming, browsing=excluded.browsing",
                        (user, day, t.active, t.gaming, t.browsing),
                    )
                for user, day in self._dirty_apps:
                    for app, secs in self._apps.get((user, day), {}).items():
                        self._db.execute(
                            "INSERT INTO app_usage(user, day, app, seconds) VALUES(?,?,?,?) "
                            "ON CONFLICT(user, day, app) DO UPDATE SET seconds=excluded.seconds",
                            (user, day, app, secs),
                        )
                for user, day in self._dirty_sites:
                    for domain, secs in self._sites.get((user, day), {}).items():
                        self._db.execute(
                            "INSERT INTO site_usage(user, day, domain, seconds) VALUES(?,?,?,?) "
                            "ON CONFLICT(user, day, domain) DO UPDATE SET seconds=excluded.seconds",
                            (user, day, domain, secs),
                        )
                for user, day in self._dirty_rules:
                    for rule, secs in self._rules.get((user, day), {}).items():
                        self._db.execute(
                            "INSERT INTO rule_usage(user, day, rule, seconds) VALUES(?,?,?,?) "
                            "ON CONFLICT(user, day, rule) DO UPDATE SET seconds=excluded.seconds",
                            (user, day, rule, secs),
                        )
                self._db.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001 - whatever went wrong, never leave the transaction open
                log.error("store flush failed: %s", exc, exc_info=not isinstance(exc, sqlite3.Error))
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return
            self._dirty_usage.clear()
            self._dirty_apps.clear()
            self._dirty_sites.clear()
            self._dirty_rules.clear()
            self._hourly.clear()
            self._evict()

    def _newest_day(self) -> Optional[date]:
        """The latest day with usage on record (in the database or not yet flushed), or None."""
        days = [k[1] for k in self._dirty_usage]
        row = self._db.execute("SELECT MAX(day) FROM usage").fetchone()
        if row and row[0]:
            days.append(str(row[0]))
        for text in sorted(days, reverse=True):
            try:
                return date.fromisoformat(text)
            except ValueError:
                continue
        return None

    def purge(self, retention_days: int, today: Optional[date] = None) -> None:
        """Delete what is older than ``retention_days``.

        The days are counted back from today - or from the newest day with usage on
        record, when that is earlier: a clock set far ahead (by mistake, a flat CMOS
        battery or on purpose) must not make the real recent history look old."""
        if retention_days <= 0:
            return
        today = today or date.today()
        with self._lock:
            try:
                newest = self._newest_day()
                reference = min(today, newest) if newest is not None else today
                cutoff = (reference - timedelta(days=retention_days)).isoformat()
                for table in ("usage", "app_usage", "site_usage", "rule_usage"):
                    self._db.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
                event_cutoff = time.time() - retention_days * 86400
                if reference < today:
                    event_cutoff -= (today - reference).days * 86400
                self._db.execute("DELETE FROM events WHERE ts < ?", (event_cutoff,))
                hourly_cutoff = reference - timedelta(days=min(retention_days, HOURLY_KEEP_DAYS))
                self._db.execute("DELETE FROM usage_hourly WHERE hour < ?", (hourly_cutoff.isoformat() + "T00",))
                # day-scoped settings end in an ISO date: bonus:<day>, limit:<day>, rollover:<day>,
                # bedtime_ext:<day>, reward:<slug>:<day>, earned:<day>
                for key in [k for k in self._settings if _dated_key_day(k[1]) is not None and _dated_key_day(k[1]) < cutoff]:
                    self.delete_setting(*key)
                # drop stale in-memory day buckets
                for cache in (self._usage, self._apps, self._sites, self._rules):
                    for key in [k for k in cache if k[1] < cutoff]:
                        cache.pop(key, None)
            except sqlite3.Error as exc:
                log.warning("store purge failed: %s", exc)
                return
            self._evict(today)
            if self.path != ":memory:":
                try:
                    self._db.execute("PRAGMA optimize")
                    self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error as exc:
                    log.debug("store maintenance after purge failed: %s", exc)

    def _evict(self, today: Optional[date] = None) -> None:
        """Forget cached day buckets older than ``CACHE_DAYS`` that hold nothing unwritten
        (lock held); they are read from the database again when asked for."""
        horizon = ((today or date.today()) - timedelta(days=CACHE_DAYS)).isoformat()
        for cache, dirty in ((self._usage, self._dirty_usage), (self._apps, self._dirty_apps),
                             (self._sites, self._dirty_sites), (self._rules, self._dirty_rules)):
            for key in [k for k in cache if k[1] < horizon and k not in dirty]:
                del cache[key]

    def close(self) -> None:
        with self._lock:
            self.flush()
            self._db.close()
