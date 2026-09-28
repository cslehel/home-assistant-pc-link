"""Constants for the PC Link integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "pclink"
MANUFACTURER = "home-assistant-pc-link"

CONF_TOKEN = "token"
DEFAULT_PORT = 47392

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

# Fired on the Home Assistant event bus for every PC / user event the service reports.
EVENT_TYPE = "pclink_event"

# How often the REST fallback polls while the WebSocket is down (seconds).
FALLBACK_POLL_SECONDS = 60
# A WebSocket that has not delivered a state push for this long is treated as dead
# (the PC pushes every ~5 s; aiohttp heartbeats catch dead TCP, this catches a stuck PC).
STALE_PUSH_SECONDS = 90
# Minimum spacing between entity-catalog refreshes triggered by state changes.
CATALOG_REFRESH_MIN_SECONDS = 20

# Device triggers, mirrored from pclink/entities.py.  The PC also sends these in its
# catalog (``triggers``); this copy is what translations and tests are keyed on.
USER_TRIGGERS = [
    "time_up", "warning", "grace_started", "logged_in", "logged_off", "login_blocked", "login_unblocked",
    "bedtime", "focus_started", "focus_ended", "break_due", "app_limit_reached", "app_closed",
    "reward_claimed", "screenshot_taken", "presence_away", "presence_back",
]
PC_TRIGGERS = [
    "started", "config_reloaded", "parent_unlocked", "pin_wrong", "pin_locked_out", "reboot_pending",
    "updates_available", "usb_changed", "printer_error",
]
