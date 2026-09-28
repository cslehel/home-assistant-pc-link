"""Home Assistant PC Link.

A Windows service (plus a Home Assistant integration in ``custom_components/``)
that bridges a (multiseat) Windows PC to Home Assistant over its local API or
MQTT: per-user screen-time limits (daily, weekly, bedtime, per app / site),
webcam-free presence detection, login blocking, telemetry, hardware sensors,
power control, activity tracking (gaming / browsing / now playing), native HA
device triggers, a multi-PC roll-up and an optional local API.
"""

__version__ = "0.3.0"
