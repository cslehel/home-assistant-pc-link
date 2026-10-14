"""Usage on the other PCs ("fleet") for shared budgets.

With ``time_limits.shared_budget: true`` a user's minutes on other PCs count
against the same daily / weekly limit.  The Home Assistant integration knows
every PC's state and passes each PC the usage of the others (the
``peer_usage`` command); this module keeps those figures.  (The "all PCs"
roll-up device is the integration's own.)

All the logic here is pure and unit-tested.
"""
from __future__ import annotations

import logging
import math
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Optional

from .config import FleetConfig

log = logging.getLogger(__name__)

# Peer data comes from another PC by way of Home Assistant: nothing in it is
# trusted, and the amount we keep is bounded.
MAX_PEERS = 64
MAX_USERS_PER_PEER = 64
MAX_ID_LENGTH = 64
MAX_DAY_MINUTES = 1440.0
MAX_HISTORY_ENTRIES = 400
MAX_EXTRAS_DAYS = 3
MAX_USAGE_DAYS = 16       # per-day figures remembered per peer and user
KEEP_USAGE_DAYS = 8        # a silent peer's usage still counts for this week's budget
PRUNE_INTERVAL_SECONDS = 60.0


# what a peer's user payload needs to hold for a shared budget (see FleetTracker.update_peers)
_USAGE_KEYS = ("name", "date", "used_minutes", "gaming_minutes", "browsing_minutes", "history",
               "bonus_local_minutes", "bedtime_extension_local_minutes")


def _number(value: Any) -> Optional[float]:
    """A finite number from a peer payload, or None.  (An int too large for a float -
    ``10**400`` - is not one: ``math.isfinite`` would raise on it.)"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _minutes(value: Any) -> float:
    """A minutes figure from a peer payload: a finite, non-negative number
    (at most one day), anything else counts as 0."""
    number = _number(value)
    if number is None or number <= 0:
        return 0.0
    return min(number, MAX_DAY_MINUTES)


def _signed_minutes(value: Any) -> float:
    """A bonus from a peer payload: a parent can take minutes away as well as give
    them, so it keeps its sign (at most a day either way); anything else counts as 0."""
    number = _number(value)
    if number is None:
        return 0.0
    return max(-MAX_DAY_MINUTES, min(number, MAX_DAY_MINUTES))


def _iso_day(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class PeerUser:
    payload: dict[str, Any]
    seen: float


@dataclass
class Peer:
    device_id: str
    last_seen: float = 0.0
    name: Optional[str] = None
    users: dict[str, PeerUser] = field(default_factory=dict)
    # user -> {ISO day: minutes}, the most this PC ever reported for that day.  Kept apart
    # from the payloads because it only ever grows: a payload with lower figures or one
    # without a history cannot hand a shared budget back.
    usage: dict[str, dict[str, float]] = field(default_factory=dict)
    # user -> {ISO day: (bonus minutes, bedtime extension minutes)} granted on that PC, the
    # latest figures it reported for the day.  Unlike usage these can go down again.
    extras: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)


class FleetTracker:
    def __init__(self, cfg: FleetConfig, device_id: str, clock: Callable[[], float] = time.time,
                 own_name: Optional[str] = None):
        """``own_name`` is this PC's ``device.name`` (default: the computer name).  A peer
        that calls itself the same is this PC under an earlier ``device.id`` and is not
        counted as another PC."""
        self.cfg = cfg
        self.device_id = device_id
        self.clock = clock
        if own_name is None:
            try:
                own_name = socket.gethostname()
            except OSError:
                own_name = ""
        self.own_name = str(own_name or "").strip().lower()
        self._former_self_warned: set[str] = set()
        self._peers: dict[str, Peer] = {}
        # update_peers runs on the API thread, everything else on the main loop
        self._lock = threading.RLock()
        self._heard = False
        self._last_prune = 0.0
        self._cap_warned = 0.0

    def ready(self) -> bool:
        """Do we know what the other PCs have recorded?  True once Home Assistant
        passed the usage of the other PCs.  Until then "no usage elsewhere" must
        not be assumed - see ``TimeKeeper.rollover_seconds``."""
        with self._lock:
            return self._heard

    def _prune(self, now: float, force: bool = False) -> None:
        """Forget peers that were silent for longer than the timeout and hold no usage
        that still counts towards a budget (lock held)."""
        if not force and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        try:
            oldest = date.fromtimestamp(now) - timedelta(days=KEEP_USAGE_DAYS)
        except (OverflowError, OSError, ValueError):
            return
        timeout = float(self.cfg.peer_timeout_seconds)
        oldest_iso = oldest.isoformat()
        for dev, peer in list(self._peers.items()):
            for user, marks in list(peer.usage.items()):
                for day_iso in [d for d in marks if d < oldest_iso]:
                    del marks[day_iso]
                if not marks:
                    del peer.usage[user]
            if now - peer.last_seen <= timeout:
                continue
            for user, pu in list(peer.users.items()):
                day = _iso_day(pu.payload.get("date"))
                if day is None or day < oldest:
                    del peer.users[user]
            if not peer.users and not peer.usage:
                del self._peers[dev]

    def _record_usage(self, peer: Peer, user: str, data: dict[str, Any], now: float) -> None:
        """Raise the per-day high-water marks of ``user`` on ``peer`` from one payload (lock held).

        Days after tomorrow are ignored (tomorrow: a peer in a time zone ahead of ours),
        so made-up dates cannot push the real ones out of the days that are kept."""
        try:
            last = date.fromtimestamp(max(now, time.time())) + timedelta(days=1)
        except (OverflowError, OSError, ValueError):
            return
        first = date.min
        marks = peer.usage.get(user)
        if marks is None:
            if len(peer.usage) >= MAX_USERS_PER_PEER:
                return
            marks = {}
        day = _iso_day(data.get("date"))
        if day is not None and first <= day <= last:
            key = day.isoformat()
            marks[key] = max(marks.get(key, 0.0), _minutes(data.get("used_minutes")))
        hist = data.get("history")
        for entry in (hist if isinstance(hist, list) else [])[:MAX_HISTORY_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            d = _iso_day(entry.get("date"))
            if d is not None and first <= d <= last:
                key = d.isoformat()
                marks[key] = max(marks.get(key, 0.0), _minutes(entry.get("active_minutes")))
        if len(marks) > MAX_USAGE_DAYS:   # keep the most recent days only
            for key in sorted(marks)[:-MAX_USAGE_DAYS]:
                del marks[key]
        if marks:
            peer.usage[user] = marks

    def _record_extras(self, peer: Peer, user: str, data: dict[str, Any]) -> None:
        """Take the bonus and the bedtime extension a peer granted (``bonus_local_minutes``,
        ``bedtime_extension_local_minutes``) for the day its payload is about (lock held).
        A payload that names neither leaves what is known alone."""
        if "bonus_local_minutes" not in data and "bedtime_extension_local_minutes" not in data:
            return
        day = _iso_day(data.get("date"))
        if day is None:
            return
        days = peer.extras.get(user)
        if days is None:
            if len(peer.extras) >= MAX_USERS_PER_PEER:
                return
            days = peer.extras[user] = {}
        key = day.isoformat()
        bonus, extension = days.get(key, (0.0, 0.0))
        if "bonus_local_minutes" in data:
            bonus = _signed_minutes(data.get("bonus_local_minutes"))
        if "bedtime_extension_local_minutes" in data:
            extension = _minutes(data.get("bedtime_extension_local_minutes"))
        days[key] = (bonus, extension)
        for old in sorted(days)[:-MAX_EXTRAS_DAYS]:   # today, last night and a day to spare
            del days[old]

    def external_extras(self, user: str, day: date) -> dict[str, Any]:
        """What the *other* PCs granted ``user`` for ``day``: ``{"bonus_seconds": the sum of
        their bonus minutes, "bedtime_extension_minutes": the longest manual bedtime
        extension}``.  Each peer's latest figures for that day count (a parent may take a
        bonus back); figures of another day do not.  A bonus can be negative (minutes taken
        away on another PC), so the sum - and ``bonus_seconds`` - can be too; all peers
        together cannot give or take more than a day."""
        user, key = user.lower(), day.isoformat()
        bonus = extension = 0.0
        with self._lock:
            for peer in self._peers.values():
                figures = peer.extras.get(user, {}).get(key)
                if figures is None or self._former_self(peer):
                    continue
                bonus += figures[0]
                extension = max(extension, figures[1])
        bonus = max(-MAX_DAY_MINUTES, min(bonus, MAX_DAY_MINUTES))
        return {"bonus_seconds": int(round(bonus * 60)),
                "bedtime_extension_minutes": int(min(extension, MAX_DAY_MINUTES))}

    def _former_self(self, peer: Peer) -> bool:
        """Is ``peer`` this PC under an earlier ``device.id``?  (lock held)"""
        if not self.own_name or not peer.name or peer.name.strip().lower() != self.own_name:
            return False
        if peer.device_id not in self._former_self_warned and len(self._former_self_warned) < MAX_PEERS:
            self._former_self_warned.add(peer.device_id)
            log.warning("fleet: %r has this PC's name: taken for this PC under an earlier device.id and not "
                        "counted as another PC (remove it from Home Assistant to drop it)", peer.device_id)
        return True

    def _peer_for(self, dev: str, now: float) -> Optional[Peer]:
        peer = self._peers.get(dev)
        if peer is not None:
            return peer
        if len(self._peers) >= MAX_PEERS:
            self._prune(now, force=True)
        if len(self._peers) >= MAX_PEERS:
            if now - self._cap_warned >= 3600 or now < self._cap_warned:
                self._cap_warned = now
                log.warning("fleet: more than %d peers; ignoring %r", MAX_PEERS, dev)
            return None
        peer = Peer(device_id=dev)
        self._peers[dev] = peer
        return peer

    def update_peers(self, peers: Any) -> int:
        """Usage of the other PCs as Home Assistant reports it (the ``peer_usage`` command):
        ``{device_id: {"name": ..., "users": {user: {"date", "used_minutes", "history", ...}}}}``.

        They are *merged*: a PC or user that is missing from a message keeps what
        was last known (Home Assistant has nothing to say about a PC that was off
        when it started), so a gap can never hand a shared budget back.  Every
        figure is sanitised and bounded.  Returns the number of peers taken."""
        taken = 0
        with self._lock:
            now = self.clock()
            self._heard = True   # Home Assistant has spoken: what it did not mention is not known to it either
            for dev, info in list(peers.items())[:MAX_PEERS] if isinstance(peers, dict) else []:
                dev = str(dev)
                if not dev or len(dev) > MAX_ID_LENGTH or dev == self.device_id or not isinstance(info, dict):
                    continue
                try:
                    if self._update_peer(dev, info, now):
                        taken += 1
                except Exception as exc:  # noqa: BLE001 - one odd payload must not cost the other PCs' figures
                    log.warning("fleet: ignoring what was sent about %r: %s", dev, exc)
        return taken

    def _update_peer(self, dev: str, info: dict[str, Any], now: float) -> bool:
        """Take one peer's figures (lock held); False when the peer is not kept."""
        self._prune(now)
        peer = self._peer_for(dev, now)
        if peer is None:
            return False
        peer.last_seen = now
        name = info.get("name")
        if isinstance(name, str) and name.strip():
            peer.name = name.strip()[:MAX_ID_LENGTH]
        users = info.get("users")
        for key, data in list(users.items())[:MAX_USERS_PER_PEER] if isinstance(users, dict) else []:
            if not isinstance(data, dict):
                continue
            user = str(data.get("user") or key).lower()[:MAX_ID_LENGTH]
            old = peer.users.get(user)
            if not user or (old is None and len(peer.users) >= MAX_USERS_PER_PEER):
                continue
            figures = {k: data[k] for k in _USAGE_KEYS if k in data}
            if isinstance(figures.get("history"), list):
                figures["history"] = figures["history"][:MAX_HISTORY_ENTRIES]
            merged = {**(old.payload if old else {}), **figures, "user": user}
            peer.users[user] = PeerUser(payload=merged, seen=now)
            self._record_usage(peer, user, figures, now)
            self._record_extras(peer, user, figures)
        return True

    # -- queries ---------------------------------------------------------------------------
    def peers(self) -> dict[str, Peer]:
        with self._lock:
            return dict(self._peers)

    def external_usage(self, user: str, day: date, week_start: date) -> tuple[float, float]:
        """Seconds the user spent *on other PCs* today and since ``week_start``.

        Each peer's figures are sanitised on their own (non-numeric, NaN and
        negative values count as 0, a day is at most 24 h) before they are
        added up, so one bogus payload can neither raise nor cancel another
        peer's usage.  What counts for a day is the *most* a peer ever reported
        for it: lower figures never give time back.  All peers together count
        for at most 24 h a day, however many there are."""
        user = user.lower()
        first, last = week_start.isoformat(), day.isoformat()
        days: dict[str, float] = {}
        with self._lock:
            for peer in self._peers.values():
                marks = peer.usage.get(user)
                if not marks or self._former_self(peer):
                    continue
                for key, minutes in marks.items():
                    if first <= key <= last or key == last:
                        days[key] = min(MAX_DAY_MINUTES, days.get(key, 0.0) + minutes)
        today_s = days.get(last, 0.0) * 60
        week_s = sum(minutes for key, minutes in days.items() if first <= key <= last) * 60
        return today_s, week_s
