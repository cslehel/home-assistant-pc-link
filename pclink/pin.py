"""Parent PIN: a second factor before Home Assistant can *loosen* a limit.

Anyone who can press a button on the HA dashboard (a child with the family
tablet, say) could otherwise grant themselves time.  With a PIN configured,
loosening actions - granting minutes, raising today's limit, pausing a limit,
unblocking, ending focus mode, extending bedtime, taking a screenshot - are
refused unless the PIN was entered recently (the ``Parent PIN`` text entity or
a ``"pin"`` field in a JSON command).  Tightening actions never need a PIN.

The PIN is compared in constant time against a SHA-256 hash; wrong attempts are
rate limited and logged; the PIN itself is never published.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Callable, Optional

from .config import TimeLimitsConfig

log = logging.getLogger(__name__)

# action kinds -> the pin_required_for group they belong to
KINDS = {
    "grant": "grant",          # add / set bonus, raise today's or weekly limit, rewards
    "unblock": "unblock",      # un-pause access
    "disable": "disable",      # disable a user's limit, global limits off, disable a rule
    "focus_off": "focus_off",  # end focus mode early
    "bedtime": "bedtime",      # extend bedtime
    "screenshot": "screenshot",
}


def hash_pin(pin: str) -> str:
    return hashlib.sha256(str(pin).strip().encode("utf-8")).hexdigest()


class ParentPin:
    def __init__(self, cfg: TimeLimitsConfig, clock: Callable[[], float] = time.time, on_event=None):
        self.cfg = cfg
        self.clock = clock
        self._on_event = on_event
        self._unlock_until = 0.0
        self._failures = 0
        self._lockout_until = 0.0
        self._digest: Optional[str] = None
        self.update_config(cfg)

    def update_config(self, cfg: TimeLimitsConfig) -> None:
        self.cfg = cfg
        if cfg.parent_pin_sha256:
            self._digest = cfg.parent_pin_sha256.strip().lower()
        elif cfg.parent_pin:
            self._digest = hash_pin(cfg.parent_pin)
        else:
            self._digest = None

    # -- state -------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._digest is not None

    def requires(self, kind: str) -> bool:
        return self.enabled and kind in self.cfg.pin_required_for

    def unlocked(self) -> bool:
        return self.enabled and self.clock() < self._unlock_until

    def locked_out(self) -> bool:
        return self.clock() < self._lockout_until

    def lock(self) -> None:
        self._unlock_until = 0.0

    # -- actions -----------------------------------------------------------------
    def try_unlock(self, pin: Any) -> bool:
        if not self.enabled:
            return True
        now = self.clock()
        if now < self._lockout_until:
            log.warning("parent PIN rejected: locked out for %d more seconds", int(self._lockout_until - now))
            self._emit("pin_locked_out")
            return False
        ok = hmac.compare_digest(hash_pin("" if pin is None else str(pin)), self._digest or "")
        if ok:
            self._failures = 0
            self._unlock_until = now + max(10, int(self.cfg.pin_timeout_seconds))
            log.info("parent PIN accepted; unlocked for %ss", self.cfg.pin_timeout_seconds)
            self._emit("parent_unlocked")
            return True
        self._failures += 1
        log.warning("wrong parent PIN (%d/%d)", self._failures, self.cfg.pin_max_attempts)
        self._emit("pin_wrong")
        if self._failures >= max(1, int(self.cfg.pin_max_attempts)):
            self._lockout_until = now + max(10, int(self.cfg.pin_lockout_seconds))
            self._failures = 0
            self._emit("pin_locked_out")
        return False

    def authorize(self, kind: str, pin: Any = None) -> tuple[bool, str]:
        """Return (allowed, reason).  An inline ``pin`` unlocks first."""
        if not self.requires(kind):
            return True, ""
        if pin is not None and str(pin) != "":
            if self.try_unlock(pin):
                return True, ""
            return False, "locked out" if self.locked_out() else "wrong pin"
        if self.unlocked():
            return True, ""
        return False, "parent pin required"

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "enabled": self.enabled,
            "unlocked": self.unlocked(),
            "unlock_seconds_left": max(0, int(self._unlock_until - now)) if self.enabled else None,
            "locked_out": self.locked_out(),
            "lockout_seconds_left": max(0, int(self._lockout_until - now)),
            "required_for": list(self.cfg.pin_required_for) if self.enabled else [],
        }

    def _emit(self, kind: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind)
            except Exception:  # noqa: BLE001
                log.debug("pin event handler failed", exc_info=True)
