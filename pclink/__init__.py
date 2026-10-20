"""Home Assistant PC Link.

A Windows service (plus a Home Assistant integration in ``custom_components/``)
that bridges a (multiseat) Windows PC to Home Assistant over its local API:
per-user screen-time limits (daily, weekly, bedtime, per app / site),
webcam-free presence detection, login blocking, telemetry, hardware sensors,
power control, activity tracking (gaming / browsing / now playing), native HA
device triggers and shared budgets across PCs.
"""

__version__ = "0.4.0"
