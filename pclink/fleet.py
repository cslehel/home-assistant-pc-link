"""Multi-PC roll-up ("fleet").

Every PC running this service publishes under ``<base_topic>/<device_id>/...``.
By subscribing to ``<base_topic>/+/availability`` and
``<base_topic>/+/user/+/state`` a service learns what every *other* PC is
doing, which gives two things without any Home Assistant configuration:

* **shared budgets** - with ``time_limits.shared_budget: true`` a user's minutes
  on other PCs count against the same daily / weekly limit;
* a **roll-up device** in HA ("PC Link (all PCs)") with per-user totals across
  all PCs, which PC each user is on, and how many PCs are online.

The roll-up is published by exactly one PC at a time - the online peer with
the lexicographically smallest device id - so the entities never fight.  All
the logic here is pure and unit-tested; MQTT plumbing lives in ``ha.py``.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from .config import FleetConfig

log = logging.getLogger(__name__)

FLEET_NODE = "fleet"   # reserved device id for the roll-up topics


@dataclass
class PeerUser:
    payload: dict[str, Any]
    seen: float


@dataclass
class Peer:
    device_id: str
    online: bool = False
    last_seen: float = 0.0
    name: Optional[str] = None
    users: dict[str, PeerUser] = field(default_factory=dict)


class FleetTracker:
    def __init__(self, cfg: FleetConfig, base_topic: str, device_id: str, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.base_topic = base_topic
        self.device_id = device_id
        self.clock = clock
        self._peers: dict[str, Peer] = {}
        self.changed = False

    # -- topics ---------------------------------------------------------------------
    def topics(self) -> list[str]:
        return [f"{self.base_topic}/+/availability", f"{self.base_topic}/+/user/+/state", f"{self.base_topic}/+/state"]

    def rollup_topic(self) -> str:
        return f"{self.base_topic}/{FLEET_NODE}/state"

    def rollup_user_topic(self, slug: str) -> str:
        return f"{self.base_topic}/{FLEET_NODE}/user/{slug}/state"

    # -- ingest -------------------------------------------------------------------------
    def on_message(self, topic: str, payload: Any) -> bool:
        """Feed a raw MQTT message; returns True when it came from a peer."""
        prefix = self.base_topic + "/"
        if not topic.startswith(prefix):
            return False
        rest = topic[len(prefix):].split("/")
        if len(rest) < 2:
            return False
        dev = rest[0]
        if dev in (self.device_id, FLEET_NODE):
            return False
        now = self.clock()
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload or "")
        peer = self._peers.get(dev)
        if peer is None:
            peer = Peer(device_id=dev)
            self._peers[dev] = peer
        if rest[1] == "availability" and len(rest) == 2:
            online = text.strip().lower() == "online"
            if online != peer.online:
                self.changed = True
            peer.online = online
            peer.last_seen = now
            return True
        if rest[1] == "state" and len(rest) == 2:
            try:
                data = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                return True
            peer.last_seen = now
            info = data.get("service_info") if isinstance(data, dict) else None
            if isinstance(data, dict):
                peer.name = data.get("device_name") or peer.name
            if isinstance(info, dict):
                peer.name = peer.name or info.get("device_name")
            return True
        if rest[1] == "user" and len(rest) == 4 and rest[3] == "state":
            try:
                data = json.loads(text) if text.strip() else None
            except json.JSONDecodeError:
                return True
            if not isinstance(data, dict):
                peer.users.pop(rest[2], None)
                self.changed = True
                return True
            user = str(data.get("user") or rest[2]).lower()
            old = peer.users.get(user)
            peer.users[user] = PeerUser(payload=data, seen=now)
            peer.last_seen = now
            if old is None or _summary(old.payload) != _summary(data):
                self.changed = True
            return True
        return False

    # -- queries ---------------------------------------------------------------------------
    def peers(self) -> dict[str, Peer]:
        return dict(self._peers)

    def online_peers(self) -> list[str]:
        """Peers whose retained availability says online and that were heard from recently.

        Availability is a retained LWT message, so it is reliable on its own;
        the timeout only guards against a broker that lost the LWT."""
        now = self.clock()
        return sorted(p.device_id for p in self._peers.values()
                      if p.online and now - p.last_seen <= self.cfg.peer_timeout_seconds)

    def is_leader(self) -> bool:
        online = self.online_peers()
        return not online or self.device_id <= min(online)

    def external_usage(self, user: str, day: date, week_start: date) -> tuple[float, float]:
        """Seconds the user spent *on other PCs* today and since ``week_start``."""
        user = user.lower()
        today_s = 0.0
        week_s = 0.0
        for peer in self._peers.values():
            pu = peer.users.get(user)
            if pu is None:
                continue
            p = pu.payload
            used_today = float(p.get("used_minutes") or 0) * 60
            p_day = p.get("date")
            if p_day == day.isoformat():
                today_s += used_today
            hist = p.get("history") or []
            counted_today = False
            for entry in hist:
                try:
                    d = date.fromisoformat(str(entry.get("date")))
                except (TypeError, ValueError):
                    continue
                if week_start <= d <= day:
                    if d == day:
                        counted_today = True
                    week_s += float(entry.get("active_minutes") or 0) * 60
            if p_day == day.isoformat() and not counted_today:
                week_s += used_today
        return today_s, week_s

    def rollup(self, own_users: dict[str, dict[str, Any]], own_online: bool = True) -> dict[str, Any]:
        """Combine our own user payloads with every peer's into one fleet view."""
        pcs: dict[str, dict[str, Any]] = {
            self.device_id: {"online": own_online, "name": self.device_id, "users_active": [], "users_logged_in": []}
        }
        for dev, peer in self._peers.items():
            pcs[dev] = {"online": peer.online, "name": peer.name or dev, "users_active": [], "users_logged_in": []}
        users: dict[str, dict[str, Any]] = {}

        def add(dev: str, user: str, p: dict[str, Any]) -> None:
            entry = users.setdefault(user, {
                "user": user, "name": p.get("name") or user, "total_minutes_today": 0.0, "gaming_minutes": 0.0,
                "browsing_minutes": 0.0, "per_pc": {}, "active_on": [], "logged_in_on": [], "status": "logged_off",
                "presence": "none", "remaining_minutes": None, "managed_on": [],
            })
            used = float(p.get("used_minutes") or 0)
            entry["total_minutes_today"] += used
            entry["gaming_minutes"] += float(p.get("gaming_minutes") or 0)
            entry["browsing_minutes"] += float(p.get("browsing_minutes") or 0)
            entry["per_pc"][dev] = round(used, 1)
            status = str(p.get("status") or "logged_off")
            if p.get("logged_in"):
                entry["logged_in_on"].append(dev)
                pcs[dev]["users_logged_in"].append(user)
            if status in ("active", "idle", "locked", "away"):
                if status == "active":
                    entry["active_on"].append(dev)
                    pcs[dev]["users_active"].append(user)
            if _STATUS_RANK.get(status, 9) < _STATUS_RANK.get(entry["status"], 9):
                entry["status"] = status
            pres = str(p.get("presence") or "none")
            if _PRESENCE_RANK.get(pres, 9) < _PRESENCE_RANK.get(entry["presence"], 9):
                entry["presence"] = pres
            if p.get("managed"):
                entry["managed_on"].append(dev)
                rem = p.get("remaining_minutes")
                if rem is not None:
                    cur = entry["remaining_minutes"]
                    entry["remaining_minutes"] = rem if cur is None else min(cur, rem)

        for user, p in own_users.items():
            add(self.device_id, user.lower(), p)
        for dev, peer in self._peers.items():
            for user, pu in peer.users.items():
                add(dev, user, pu.payload)
        for entry in users.values():
            entry["total_minutes_today"] = round(entry["total_minutes_today"], 1)
            entry["gaming_minutes"] = round(entry["gaming_minutes"], 1)
            entry["browsing_minutes"] = round(entry["browsing_minutes"], 1)
            entry["active_on"].sort()
            entry["logged_in_on"].sort()
        online = sorted(d for d, p in pcs.items() if p["online"])
        in_use = sorted(d for d, p in pcs.items() if p["online"] and p["users_active"])
        return {
            "leader": self.device_id,
            "pcs": pcs,
            "pcs_online": len(online),
            "pcs_online_list": online,
            "pcs_in_use": len(in_use),
            "pcs_in_use_list": in_use,
            "any_pc_in_use": bool(in_use),
            "users": users,
            "user_count": len(users),
            "active_users": sorted(u for u, e in users.items() if e["active_on"]),
        }


_STATUS_RANK = {"active": 0, "idle": 1, "away": 2, "locked": 3, "disconnected": 4, "logged_off": 5}
_PRESENCE_RANK = {"active": 0, "passive": 1, "idle": 2, "away": 3, "none": 4}


def _summary(p: dict[str, Any]) -> tuple:
    return (p.get("used_minutes"), p.get("status"), p.get("presence"), p.get("logged_in"), p.get("remaining_minutes"),
            p.get("gaming_minutes"), p.get("browsing_minutes"), p.get("date"))
