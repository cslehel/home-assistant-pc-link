"""Embedded copy of config.example.yaml (fallback for init-config)."""

EXAMPLE_CONFIG = r'''# ======================================================================
# Home Assistant PC Link - configuration
# ----------------------------------------------------------------------
# Copy to  C:\ProgramData\PCLink\config.yaml  (or run: python -m pclink init-config)
# Validate with:  python -m pclink check-config
# ======================================================================

mqtt:
  host: homeassistant.local      # your MQTT broker (usually the HA host)
  port: 1883
  username: pclink               # broker credentials (recommended)
  password: change-me
  # tls: true
  # tls_ca: C:\ProgramData\PCLink\ca.crt
  discovery_prefix: homeassistant # must match HA's MQTT discovery prefix
  base_topic: pclink

device:
  # name: Family-PC              # defaults to the Windows computer name
  # suggested_area: Living room

telemetry:
  interval_seconds: 15           # how often PC metrics are published
  disks: true
  network: true
  gpu: true

hardware_sensors:
  enabled: true
  backend: auto                  # auto -> LibreHardwareMonitor DLL, then its web server
  # lhm_dll_path: C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitorLib.dll
  interval_seconds: 10
  sensor_types: [temperature, fan, power, load]
  cpu: true
  gpu: true
  motherboard: true
  storage: true                  # SMART data; polled every few minutes only

# ----------------------------------------------------------------------
# Time limits. One block per Windows user account (the SAM login name).
# ASTER seats are ordinary Windows sessions, so several users can be
# active at once and each is tracked and limited independently.
# ----------------------------------------------------------------------
time_limits:
  enabled: true
  # How login blocking is enforced when a user is out of time:
  #   logon_hours     - set the account's permitted logon hours to none (default)
  #   disable_account - set the account-disabled flag
  #   logoff_only     - do not block login, only log the user off
  block_method: logon_hours
  manage_unknown_users: false    # apply default_policy to users not listed below

  default_policy:                # used for new users when manage_unknown_users: true
    daily_limit_minutes: 120
    action: logoff
    warn_at_minutes: [15, 5, 1]

  users:
    - name: kevin                # <- Windows account name
      display_name: Kevin
      daily_limit_minutes:
        weekdays: 90             # Mon-Fri
        weekend: 180             # Sat-Sun
        fri: 150                 # a single day overrides the group
      allowed_hours:
        all: 07:00-20:00         # only allowed to be logged in in this window
        weekend: 08:00-21:00
      warn_at_minutes: [30, 15, 5, 1]
      action: logoff             # logoff | lock | shutdown
      grace_seconds: 60          # warning-to-action countdown
      count_idle: false          # idle time does not consume the budget
      idle_threshold_minutes: 10

    - name: sarah
      display_name: Sarah
      daily_limit_minutes: 150
      action: lock

    - name: dad                  # parents: statistics only, no limit
      unlimited: true
    - name: mum
      unlimited: true

activity:
  idle_seconds: 120              # no input for this long = idle
  audio_keeps_active: true       # playing audio counts as active (e.g. video)
  url_method: uia                # uia | title | off  (how the active site is read)
  group_sites_by: registrable    # registrable (youtube.com) | host (m.youtube.com)
  top_n: 10
  # Categorise sites for nicer HA statistics (optional):
  site_categories:
    youtube.com: video
    netflix.com: video
    twitch.tv: video
    tiktok.com: social
    instagram.com: social
    facebook.com: social
    discord.com: social
    reddit.com: social
    roblox.com: gaming
    khanacademy.org: education
    wikipedia.org: education

power:
  allow_shutdown: true
  allow_reboot: true
  allow_sleep: true
  allow_hibernate: true
  shutdown_delay_seconds: 10

# Optional custom buttons exposed to Home Assistant (button.<device>_<name>):
# commands:
#   - name: Mute volume
#     command: nircmd.exe mutesysvolume 1
#     run_as: user               # user -> runs in each active desktop; system -> as service
#     icon: mdi:volume-mute
#   - name: Open homework folder
#     command: explorer.exe C:\Homework
#     run_as: user

agent:
  enabled: true                  # per-session activity agent + tray icon
  tray_icon: true                # show the state icon in each user's system tray
  poll_seconds: 2

logging:
  level: INFO
'''
