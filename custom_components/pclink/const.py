"""Constants for the PC Link integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "pclink"
MANUFACTURER = "home-assistant-pc-link"

CONF_TOKEN = "token"
CONF_TLS = "tls"                  # the PC serves HTTPS (api.tls_cert / api.tls_key)
CONF_FINGERPRINT = "fingerprint"  # SHA-256 of its certificate (hex); only that certificate is accepted
DEFAULT_PORT = 47392

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.UPDATE,
]

# Fired on the Home Assistant event bus for every PC / user event the service reports.
EVENT_TYPE = "pclink_event"

# How often the REST fallback polls while the WebSocket is down (seconds).
FALLBACK_POLL_SECONDS = 60
# A WebSocket that has not delivered a state push for this long is treated as dead
# (the PC pushes every ~5 s; aiohttp heartbeats catch dead TCP, this catches a stuck PC).
STALE_PUSH_SECONDS = 90
# ... and at least this many push intervals (the interval is measured from the pushes
# themselves, so a PC configured with a slow ``api.push_interval_seconds`` is not cut off).
STALE_PUSH_INTERVALS = 3
STALE_PUSH_MAX_SECONDS = 3600
# Minimum spacing between entity-catalog refreshes triggered by state changes.
CATALOG_REFRESH_MIN_SECONDS = 20
# Upper bound for that spacing after failed refreshes (it doubles per failure).
CATALOG_REFRESH_MAX_SECONDS = 600
# An entity whose key left the catalog is unavailable at once, and removed from the
# entity registry only after the catalog has kept omitting it for this long while the
# PC stayed connected (hardware sensors, for one, are missing right after the PC boots).
CATALOG_REMOVE_GRACE_SECONDS = 600
# The reconnect backoff restarts at 1 s only after a connection lasted this long.
WS_HEALTHY_SECONDS = 60

# The users' figures (date, minutes used) are written to storage at most this often; they
# are what a PC that is switched off still contributes to the roll-up after a restart.
USAGE_CACHE_SECONDS = 300
# Events the PC recorded while Home Assistant was not connected are fired after the
# reconnect: at most this many, none older than this.
EVENT_REPLAY_MAX = 200
EVENT_REPLAY_MAX_AGE_SECONDS = 86400
# A recorded event and the same event pushed live may carry time stamps this far apart.
EVENT_SAME_SECONDS = 2.0

# Repair issues (repairs.py): a different PC answers at the configured address / the PC
# announced itself at another address while it could not be reached at the configured one.
ISSUE_WRONG_PC = "wrong_pc"
ISSUE_MOVED = "pc_moved"
# Device registry identifier of the "all PCs" roll-up device.
ROLLUP_DEVICE_ID = "all_pcs"

# Unique ids of the "all PCs" roll-up entities (rollup.py).  A PC's own entities are
# ``pclink_<device id>_...`` with ids made of letters, digits and underscores, so this
# prefix can never be one of them.
ROLLUP_UNIQUE_PREFIX = "pclink-all-pcs:"

# Device triggers, mirrored from pclink/entities.py.  The PC also sends these in its
# catalog (``triggers``); this copy is what translations and tests are keyed on.
USER_TRIGGERS = [
    "time_up", "warning", "grace_started", "logged_in", "logged_off", "login_blocked", "login_unblocked", "bedtime",
    "focus_started", "focus_ended", "break_due", "app_limit_reached", "app_closed", "reward_claimed",
    "screenshot_taken", "presence_away", "presence_back", "app_close_failed", "tamper", "time_requested",
    "time_request_approved", "time_request_denied", "time_request_expired", "profile_changed", "weekly_summary",
]
PC_TRIGGERS = [
    "started", "config_reloaded", "parent_unlocked", "pin_wrong", "pin_locked_out", "reboot_pending",
    "updates_available", "usb_changed", "printer_error", "degraded", "config_invalid",
]


def issue_id(kind: str, entry_id: str) -> str:
    return f"{kind}_{entry_id}"


def device_entry_ids(device) -> list[str]:
    """Config entry ids of a device registry entry, on current and older Home Assistant
    (a device used to list several config entries; it now belongs to exactly one)."""
    entry_id = getattr(device, "config_entry_id", None)
    if entry_id:
        return [entry_id]
    return list(getattr(device, "config_entries", None) or [])
