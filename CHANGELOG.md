# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). The release workflow publishes the
section of the version being tagged as the release notes.

## [0.3.0] - 2026-09-28

First public release.

### Install

- **Standalone Windows version, no Python needed:** download
  `PCLink-0.3.0-windows-x64.zip` below, right-click it, choose **Properties**,
  tick **Unblock**, extract it and run **install.cmd**. The first run creates
  the configuration and opens it in Notepad; run **install.cmd** again after
  saving it. Details:
  [installation guide](https://github.com/cslehel/home-assistant-pc-link/blob/main/docs/INSTALL.md).
- **Home Assistant:** add this repository to HACS as a custom *Integration*
  repository and install **PC Link**, or copy `custom_components/pclink` into
  your Home Assistant configuration. Then add the PC under Settings → Devices
  & services. See the
  [Home Assistant guide](https://github.com/cslehel/home-assistant-pc-link/blob/main/docs/HOMEASSISTANT.md).
  Requires Home Assistant 2024.3 or newer.

### Screen-time control

- Per-user daily, weekday, weekend and weekly limits, allowed hours, a
  separate bedtime, and rollover of unused minutes.
- Warnings in the tray, a grace countdown, then log off, lock or shut down.
- Login blocking while a user is out of time, through logon hours or by
  disabling the account.
- Per-app, per-website and per-category limits and blocklists, focus
  (homework) mode, and earned bonus minutes.
- Optional parent PIN for anything that loosens a limit.
- Every seat of an ASTER multiseat PC is tracked and limited separately.

### Monitoring

- CPU, memory, disk, network, GPU and battery metrics; temperatures, fans and
  power through LibreHardwareMonitor.
- Time spent gaming and browsing, the current app and website, top apps and
  sites, and a 7-day history.
- Webcam-free presence detection, now playing, USB devices, printers and
  Windows Update status.

### Control from Home Assistant

- Shut down, reboot, sleep, hibernate, lock, log off, messages, volume,
  launch or close apps, Wake-on-LAN and custom commands.
- Native integration with UI setup, auto-discovery, device triggers and
  actions; MQTT discovery as an alternative.
- Time limits keep working when Home Assistant or the network is down, and
  everything resyncs on reconnect.

### Security

- The service auto-restarts and only administrators can stop or change it.
  Monitored users must be standard accounts.
- The standalone build is unsigned. If your antivirus flags it, see the
  [antivirus guide](https://github.com/cslehel/home-assistant-pc-link/blob/main/docs/ANTIVIRUS.md).
