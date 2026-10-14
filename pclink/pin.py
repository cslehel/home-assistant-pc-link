"""Parent PIN: a second factor before Home Assistant can *loosen* a limit.

Anyone who can press a button on the HA dashboard (a child with the family
tablet, say) could otherwise grant themselves time.  With a PIN configured,
loosening actions - granting minutes, raising today's limit, pausing a limit,
unblocking, starting a focus period that pauses the budget, ending focus mode,
extending bedtime, taking a screenshot - are refused unless the PIN was
entered recently (the ``Parent PIN`` text entity) or comes with the command
itself (a ``"pin"`` field in a JSON command, which authorises that one command
only).  Tightening actions never need a PIN.

The PIN is kept as a salted PBKDF2 hash (``time_limits.parent_pin_hash``; the
older plain SHA-256 ``parent_pin_sha256`` is still accepted) and compared in
constant time.  Wrong attempts are rate limited - every lockout in a row lasts
twice as long as the one before, until a correct PIN is entered - and logged;
the PIN itself is never published.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Any, Callable, Optional

from .config import TimeLimitsConfig

log = logging.getLogger(__name__)

# action kinds -> the pin_required_for group they belong to
KINDS = {
    "grant": "grant",          # add / set bonus, raise today's or weekly limit, rewards
    "unblock": "unblock",      # un-pause access
    "disable": "disable",      # disable a user's limit, global limits off, disable a rule
    "focus": "focus",          # start a focus period that pauses the time budget
    "focus_off": "focus_off",  # end focus mode early
    "bedtime": "bedtime",      # extend bedtime
    "screenshot": "screenshot",
    "parent_present": "parent_present",  # turn the "parent is home" switch on (not in the default list)
}

PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000
PBKDF2_MAX_ITERATIONS = 10_000_000   # a larger figure in the config would stall the service for minutes
MAX_LOCKOUT_SECONDS = 3600           # consecutive lockouts double up to this (or the configured one, if longer)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _pin_bytes(pin: Any) -> bytes:
    return ("" if pin is None else str(pin)).strip().encode("utf-8")


def sha256_pin(pin: Any) -> str:
    """The older, unsalted form (``time_limits.parent_pin_sha256``)."""
    return hashlib.sha256(_pin_bytes(pin)).hexdigest()


def hash_pin(pin: str, *, salt: Optional[bytes] = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    """``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` for ``time_limits.parent_pin_hash``.

    Salted and slow on purpose: a PIN has few digits, so a plain hash of it is
    reversed at once by whoever gets to read the configuration."""
    salt = secrets.token_bytes(16) if salt is None else bytes(salt)
    digest = hashlib.pbkdf2_hmac("sha256", _pin_bytes(pin), salt, int(iterations))
    return f"{PBKDF2_PREFIX}${int(iterations)}${salt.hex()}${digest.hex()}"


def verify_pin(pin: Any, stored: Optional[str]) -> bool:
    """Does ``pin`` match ``stored`` (the salted form or a plain SHA-256 hex digest)?
    Constant-time; a stored value in neither form matches nothing."""
    text = (stored or "").strip()
    if text.startswith(PBKDF2_PREFIX + "$"):
        try:
            _name, iterations, salt_hex, hash_hex = text.split("$")
            rounds, salt, expected = int(iterations), bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
        except ValueError:
            return False
        if not 1 <= rounds <= PBKDF2_MAX_ITERATIONS or not expected:
            return False
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", _pin_bytes(pin), salt, rounds), expected)
    text = text.lower()
    if not _SHA256_HEX.match(text):
        return False
    return hmac.compare_digest(sha256_pin(pin).encode("ascii"), text.encode("ascii"))


SETTING_PIN_FAILURES = "pin_failures"
SETTING_PIN_LOCKOUT_UNTIL = "pin_lockout_until"
SETTING_PIN_LOCKOUTS = "pin_lockouts"   # lockouts in a row since the last correct PIN
_GLOBAL = "_global"  # same owner key as Store.GLOBAL


class ParentPin:
    def __init__(self, cfg: TimeLimitsConfig, clock: Callable[[], float] = time.time, on_event=None, store=None):
        """``store`` (a ``pclink.store.Store``) is optional: when given, the
        failure count and the lockout survive a service restart or a reboot,
        so restarting the PC is not a way to get fresh PIN attempts."""
        self.cfg = cfg
        self.clock = clock
        self._on_event = on_event
        self._store = store
        self._unlock_until = 0.0
        self._failures = 0
        self._lockouts = 0
        self._lockout_until = 0.0
        self._digest: Optional[str] = None
        self.update_config(cfg)
        self._load_state()

    # -- persistence (optional) ----------------------------------------------------
    def _lockout_seconds(self) -> int:
        """How long the lockout in force (or the next one) lasts: the configured time,
        doubled for every lockout in a row after the first."""
        base = max(10, int(self.cfg.pin_lockout_seconds))
        doublings = min(max(0, self._lockouts - 1), 20)
        return min(base * (2 ** doublings), max(base, MAX_LOCKOUT_SECONDS))

    def _load_state(self) -> None:
        if self._store is None:
            return
        try:
            self._failures = max(0, int(self._store.get_setting(_GLOBAL, SETTING_PIN_FAILURES, 0) or 0))
            self._lockouts = max(0, int(self._store.get_setting(_GLOBAL, SETTING_PIN_LOCKOUTS, 0) or 0))
            self._lockout_until = float(self._store.get_setting(_GLOBAL, SETTING_PIN_LOCKOUT_UNTIL, 0.0) or 0.0)
        except (TypeError, ValueError):
            self._failures, self._lockouts, self._lockout_until = 0, 0, 0.0

    def _save_state(self) -> None:
        if self._store is None:
            return
        try:
            self._store.set_setting(_GLOBAL, SETTING_PIN_FAILURES, self._failures)
            self._store.set_setting(_GLOBAL, SETTING_PIN_LOCKOUTS, self._lockouts)
            self._store.set_setting(_GLOBAL, SETTING_PIN_LOCKOUT_UNTIL, self._lockout_until)
        except Exception:  # noqa: BLE001 - the in-memory lockout still applies
            log.warning("could not persist parent PIN lockout state", exc_info=True)

    def _lockout_end(self, now: float) -> float:
        """The lockout deadline, never further away than one full lockout
        (a clock that was set back must not turn it into a permanent one)."""
        if self._lockout_until - now > self._lockout_seconds():
            self._lockout_until = now + self._lockout_seconds()
            self._save_state()
        return self._lockout_until

    def update_config(self, cfg: TimeLimitsConfig) -> None:
        """Take the PIN from the configuration: ``parent_pin_hash`` (salted), else
        ``parent_pin_sha256``, else the plain ``parent_pin``.  A *changed* PIN starts
        with a clean slate - no lockout, nobody unlocked - which is also the way out
        for a parent who was locked out by somebody else's guessing."""
        self.cfg = cfg
        old = self._digest
        if getattr(cfg, "parent_pin_hash", None):
            self._digest = str(cfg.parent_pin_hash).strip()
        elif cfg.parent_pin_sha256:
            self._digest = str(cfg.parent_pin_sha256).strip().lower()
        elif cfg.parent_pin:
            self._digest = sha256_pin(cfg.parent_pin)
        else:
            self._digest = None
        if old is not None and self._digest != old:
            had_state = bool(self._failures or self._lockouts or self._lockout_until)
            self._failures, self._lockouts, self._lockout_until, self._unlock_until = 0, 0, 0.0, 0.0
            if had_state:
                self._save_state()

    # -- state -------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._digest is not None

    def requires(self, kind: str) -> bool:
        return self.enabled and kind in self.cfg.pin_required_for

    def unlocked(self) -> bool:
        if not self.enabled:
            return False
        now = self.clock()
        timeout = max(10, int(self.cfg.pin_timeout_seconds))
        if self._unlock_until - now > timeout:  # the clock was set back: do not stay unlocked for longer
            self._unlock_until = now + timeout
        return now < self._unlock_until

    def locked_out(self) -> bool:
        now = self.clock()
        return now < self._lockout_end(now)

    def lock(self) -> None:
        self._unlock_until = 0.0

    # -- actions -----------------------------------------------------------------
    def _check(self, pin: Any, now: float) -> bool:
        """Is ``pin`` right?  Counts a wrong one and starts a lockout after too many;
        while locked out even the right PIN is refused."""
        if now < self._lockout_end(now):
            log.warning("parent PIN rejected: locked out for %d more seconds", int(self._lockout_until - now))
            self._emit("pin_locked_out")
            return False
        if verify_pin(pin, self._digest):
            had_state = bool(self._failures or self._lockouts or self._lockout_until)
            self._failures = 0
            self._lockouts = 0
            self._lockout_until = 0.0
            if had_state:
                self._save_state()
            return True
        self._failures += 1
        log.warning("wrong parent PIN (%d/%d)", self._failures, self.cfg.pin_max_attempts)
        self._emit("pin_wrong")
        if self._failures >= max(1, int(self.cfg.pin_max_attempts)):
            self._lockouts += 1
            self._lockout_until = now + self._lockout_seconds()
            self._failures = 0
            log.warning("parent PIN locked for %d seconds (lockout %d in a row)", self._lockout_seconds(), self._lockouts)
            self._emit("pin_locked_out")
        self._save_state()
        return False

    def try_unlock(self, pin: Any) -> bool:
        """Enter the PIN (the ``Parent PIN`` entity): a correct one unlocks every
        guarded action for ``pin_timeout_seconds``."""
        if not self.enabled:
            return True
        now = self.clock()
        if not self._check(pin, now):
            return False
        self._unlock_until = now + max(10, int(self.cfg.pin_timeout_seconds))
        log.info("parent PIN accepted; unlocked for %ss", self.cfg.pin_timeout_seconds)
        self._emit("parent_unlocked")
        return True

    def authorize(self, kind: str, pin: Any = None) -> tuple[bool, str]:
        """Return (allowed, reason).

        A ``pin`` that comes with the command authorises this one command and
        nothing else: it does not open the unlock window, so a script that passes
        the PIN does not leave the dashboard open for whoever presses a button
        next."""
        if not self.requires(kind):
            return True, ""
        if pin is not None and str(pin) != "":
            if self._check(pin, self.clock()):
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
            "lockout_seconds_left": max(0, int(self._lockout_end(now) - now)),
            "required_for": list(self.cfg.pin_required_for) if self.enabled else [],
        }

    def _emit(self, kind: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind)
            except Exception:  # noqa: BLE001
                log.debug("pin event handler failed", exc_info=True)
