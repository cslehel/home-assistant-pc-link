# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). The release workflow publishes the
section of the version being tagged as the release notes.

## [0.4.1] - 2026-10-20

### Install

- **Standalone Windows version:** download `PCLink-0.4.1-windows-x64.zip`
  below, right-click it, choose **Properties**, tick **Unblock**, extract it
  and run **install.cmd**. An installed copy is upgraded in place and keeps
  its configuration; a PC with 0.4.0 updates itself (the *PC Link update*
  entity in Home Assistant). Details:
  [installation guide](https://github.com/cslehel/home-assistant-pc-link/blob/main/docs/INSTALL.md).

### Fixed

- Right after installing, the service could stop again at once: Windows
  refused its first "service started" message for the event log, and the
  service took that as a fatal error. The installer then reported a failed
  installation and did not add the firewall rule for the local API. A
  message the event log refuses is now only noted in PC Link's own log.

## [0.4.0] - 2026-10-20

### Install

- **Standalone Windows version:** download `PCLink-0.4.0-windows-x64.zip`
  below, right-click it, choose **Properties**, tick **Unblock**, extract it
  and run **install.cmd**. An installed copy is upgraded in place and keeps
  its configuration. Details:
  [installation guide](https://github.com/cslehel/home-assistant-pc-link/blob/main/docs/INSTALL.md).
- **Updating:** PC Link now updates itself from these releases (the *PC Link
  update* entity in Home Assistant; `updates.auto_install: true` installs new
  releases without being asked). Version 0.3.0 cannot do that yet: a PC that
  runs 0.3.0 is upgraded by hand once, as above. A private repository needs
  `updates.github_token`.
- **Home Assistant:** update PC Link in HACS, or copy
  `custom_components/pclink` into your configuration again.

### Removed

- **MQTT:** PC Link no longer talks to an MQTT broker. The local API (`api:`;
  `pclink.exe setup-api` sets it up) with the
  PC Link integration is the only way Home Assistant reaches the PC - no
  broker credentials on the PC, no broker that can take every PC offline, one
  dependency (`paho-mqtt`) less. Gone with it: MQTT discovery and device
  triggers, the MQTT availability sensor, the command topics, retained
  screenshot images (the camera reads `GET /api/users/<user>/screenshot`) and
  the MQTT fleet roll-up (the integration's "PC Link (all PCs)" device
  replaces it; shared budgets work through the integration). Removed settings:
  `mqtt:`, `fleet.publish_rollup`, `fleet.device_name`, `screenshots.retain`;
  the `test-mqtt` command; `mqtt_connected` in `GET /api/health`; the `fleet`
  key of the PC state.

### Added

- **One-step connection to Home Assistant:** `pclink.exe setup-api` turns the
  local API on for the LAN, creates a token and a certificate for HTTPS and
  writes them into `config.yaml` (the rest of the file stays as it is),
  restarts the service and prints the token. A token and certificate that are
  there already are kept. The standalone installer runs it by itself when the
  API is not set up - on a new install the configuration comes with it - and
  ends by showing the token (`setup-api --show`), so adding a discovered PC in
  Home Assistant only takes the token.
- **Rules can be edited from Home Assistant:** `pclink.set_rule` creates a
  per-app / per-site rule for a user or changes one (also one from
  `config.yaml`): programs, websites, limits per day, allowed hours, blocked.
  `pclink.remove_rule` removes it or restores the configured rule. A change
  that allows more needs the parent PIN; making or tightening a rule does not.
- **Weekly budgets per rule:** `weekly_limit_minutes` on a rule, with a
  *remaining this week* sensor and a *weekly limit* number;
  `pclink.rule_limit` takes `scope: weekly`.
- **"PC Link (all PCs)" in the integration:** with two or more PCs set up, the Home
  Assistant integration works out the roll-up device itself (PCs online and
  in use, per user the total time today, status and the PCs they are on).
- **Shared budgets through the integration:** with
  `time_limits.shared_budget: true` the integration passes each PC the usage
  of the other PCs, so one daily / weekly limit spans all of them.
- **Display switch per user:** turn the monitor of a user's seat off and on
  from Home Assistant (`pclink.display`, `agent.display_control`).
- **`time_limits.time_zone`:** days, allowed hours and bedtimes can be
  reckoned in a fixed time zone instead of the PC's own setting. A Python
  install needs the `tzdata` package (now in `requirements-windows.txt`).
- **Update PC Link from Home Assistant:** a *PC Link update* entity shows the
  installed and the latest released version and installs the update on the PC
  (standalone build); `updates.auto_install: true` does it unattended.
- **Block apps and websites from Home Assistant:** per-user *block* /
  *unblock* text entities, a *blocked apps and websites* sensor and the
  actions `pclink.block_item` / `pclink.unblock_item`.
- **Idle and away time:** sensors *idle time today* / *away time today*, and
  active / passive / idle / away / locked minutes in the usage report.
- **Usage report:** a per-user sensor *usage in the last 10 h* listing the
  applications, games and websites used in that window, the action
  `pclink.usage_report` for any other window and
  `GET /api/users/<user>/report`. Usage is now also recorded per hour.
- Now playing reads Windows' media controls in the standalone build, so music
  or video in a background browser tab is reported; Firefox's address bar is
  read correctly.
- **Wake up** button in the Home Assistant integration (Wake-on-LAN), which
  also works after Home Assistant restarted while the PC was off.
- The integration ships its own icon, and the executables have a new one.
- The integration loads from its last known state when the PC is switched off
  at Home Assistant start, instead of waiting for the PC.
- **Ask for more time:** the tray icon's menu offers "Ask for more time"
  (`time_limits.time_requests`: amounts, cooldown, expiry, daily maximum). The
  parent approves or denies from Home Assistant (`pclink.answer_time_request`,
  approve / deny buttons, the `time_requested` trigger); approving honours the
  parent PIN.
- **Profiles:** named sets of limit, allowed-hours and bedtime overrides
  (`time_limits.profiles`, per-user `profiles`), switched with a *profile*
  select or `pclink.set_profile`, per child or for everyone. Switching to a
  looser one needs the parent PIN.
- **Weekly summary:** a *time last week* sensor with minutes per day, the
  busiest day, top apps, games and websites, how often time ran out and the
  bonus granted, plus a `weekly_summary` trigger.
- **Tamper warning** per child (sensor, reason, trigger, *clear* button): the
  agent was killed or went silent, sent forged reports, or the PC's clock or
  time zone was changed.
- **Service problem** sensor: the service reports when a step keeps failing, a
  part could not start (it retries every minute) or it had to start from the
  last working configuration.
- With a shared budget, **bonus minutes and bedtime extensions** granted on
  one PC count on the others.
- **Store apps** (Calculator, Minecraft for Windows, Netflix) are recognised
  as themselves, so rules and limits work for them.
- Time warnings are shown **on top of a full-screen game** or with "do not
  disturb" on, and notifications arriving together no longer replace each
  other.
- Home Assistant integration: events missed while Home Assistant was
  restarting or disconnected are **replayed** (`replayed: true`); the
  "PC Link (all PCs)" device can be the target of actions, and actions accept
  `target:`; new hardware sensors and disks appear without a reload; the
  update entity shows install progress and release notes; switches, numbers
  and texts show a new value at once; a repair is offered when a PC announces
  itself at a new address.
- A new configuration made by `init-config` is tied to the PC's time zone
  (`time_limits.time_zone`).

### Security

- **Signed updates:** a copy of PC Link that is code-signed only installs an
  update whose executables and installer script are signed by the same
  publisher, and whose signed catalog (`pclink.cat`, made by the release
  workflow when a certificate is configured) lists exactly the files in the
  download. `updates.trusted_signers` pins
  certificates, `updates.require_signature` refuses unsigned updates. An
  update with a broken signature is always refused. Why an update failed or
  was refused is shown in the attributes of the *PC Link update* entity.
- **`api.read_token`:** a token that can only read. Once set, reading the
  local API from the PC itself needs a token too, so other users logged in on
  the PC can no longer look at it.
- With `time_limits.time_zone`, changing the PC's time zone no longer moves
  the day boundary.
- The data folder (`C:\ProgramData\PCLink`: configuration with the API token
  and parent PIN, database, logs) is now restricted to
  administrators and SYSTEM at install and at every service start. The agent
  logs to `%LOCALAPPDATA%\PCLink\logs` instead.
- A screenshot reply from the agent can no longer choose the file name the
  service saves it under.
- Parent PIN: no longer written to the service log; the lockout survives a
  restart; raising a weekly / weekday / weekend / per-app limit, removing one
  with a negative value, and starting focus mode with a custom allow-list now
  need the PIN; re-sending a limit no longer restarts the grace period.
  `parent_present` can be added to `pin_required_for`.
- Shared budgets no longer fail open on a malformed message from another PC.
- The local API refuses cross-origin WebSocket connections, and a client that
  stops reading can no longer stall the service.
- Home Assistant no longer follows an mDNS announcement to a new address
  without checking that the same PC answers there.
- A forged or malformed report from an agent can no longer stop time
  counting; it is rejected and raises the tamper warning. What an agent sends
  is limited in size, rate and number of distinct names.
- One failing step of the main loop no longer stops the others or the saving
  of usage.
- **Self-update:** the signature check fails closed (an update whose
  signatures cannot be read is refused, and the installed publisher is
  recorded at first start), a download must be the announced version and
  newer than the installed one, and an upgrade that does not start is rolled
  back.
- **Local API:** `api.admin_token` separates configuration changes, custom
  commands and update installs from everyday control; optional HTTPS
  (`api.tls_cert` / `api.tls_key`, `pclink make-cert`) with the certificate's
  fingerprint pinned by the integration; wrong tokens are throttled; the port
  is bound exclusively and the firewall rule is limited to the program;
  requests without a token are only answered under the PC's own names
  (`api.allowed_hosts` adds more); connection limits; `GET /api/auth`.
- The service and the installers refuse a data folder that is a link or
  cannot be restricted to administrators.
- A `config.yaml` that no longer loads does not stop enforcement after a
  restart: the service starts from the last working copy and reports it.
- Starting a focus period that pauses the time budget needs the parent PIN
  (kind `focus`), and a focus period is at most 24 hours.
- Shared budgets: usage on other PCs is remembered across restarts and cannot
  be reduced by an offline network or lower figures.
- Parent PIN: stored as a salted hash (`parent_pin_hash`, from
  `pclink pin-hash`; `parent_pin_sha256` still works); repeated lockouts get
  longer; a PIN passed inside a command authorises that command only.
- Programs named `python.exe`, `pclink.exe`, `pclink-agent.exe` or
  `taskmgr.exe` are no longer exempt from rules and focus mode.
- The integration checks that the PC answering is the one that was added; a
  different PC at that address is ignored and reported as a repair. Its
  diagnostics download redacts every free text.
- The build installs hash-pinned dependencies and signs in a separate,
  protected job; workflow actions are pinned to commits.
- **The agent and the service prove themselves to each other** by
  challenge-response over the per-launch token, which is no longer sent. An
  agent obeys nothing (`run`, close, screenshot, lock) from a program that took
  the agent port while the service was not running, and it exits when its
  service ends. The agent port is bound before anything slow starts.
- `launch` with a free command line needs the admin token when
  `api.admin_token` is set; in Home Assistant `pclink.command`, `pclink.launch`
  and `pclink.reload_config` are admin-only actions.
- Without `time_limits.time_zone` the service keeps the time zone it first ran
  in (`time_zone_pinned`): changing Windows' time zone no longer moves the day,
  bedtime or allowed hours.
- The first securing of the data folder moves files, links and hard links of
  other accounts into `quarantine-<time>` instead of adopting them, and the
  installer lists them before anything touches the folder.
- The API also listens on `[::1]` (or `[::]`), so no other program can take
  that address; all PC-local addresses count as one for wrong-token throttling
  and connection caps, and a reserve of connections is kept for clients with a
  token (others get 503).
- Home Assistant: the "PC moved" repair and Reconfigure send the stored token
  to a new address only while the pinned certificate stays; an announcement
  can no longer change HTTPS or the fingerprint; the forms show the
  fingerprint (or "HTTP, not encrypted"); the add form no longer shows the
  typed token again after an error.
- A renewed signing certificate must come from the same issuing CA (otherwise
  list it in `updates.trusted_signers`); the update download has an overall
  deadline; the build no longer upgrades pip.
- The setup page keeps the token per browser tab and has a "Forget token"
  button. `install-service.ps1` no longer makes a firewall rule for the port
  alone; `defender-exclusion.ps1` only excludes a Python only administrators
  can change.
- A warning is logged for LibreHardwareMonitor older than 0.9.5, which loads
  the WinRing0 driver.
- Home Assistant gets the web address without query string and fragment, and
  window titles cut to 160 characters; fast-changing attributes are kept out of
  its recorder.

### Changed

- `api.allow_config_edit` is off by default.
- With `action: lock`, "time is up" is logged and announced once per
  lock-out instead of every 30 seconds; "app closed" fires once per window.
- A blocked app that will not close is escalated: the tab, then the program,
  then the session is locked (`app_close_failed`).
- A child's `shutdown` action logs off instead while anyone else is logged
  in, including on a disconnected session.
- `retention_days` below 8 and `tick_seconds` above 60 are corrected with a
  warning; two users whose names give the same Home Assistant id are rejected.
- The login-blocked sensor reads on / off; the roll-up "total time today"
  sensors are totals that reset at midnight.
- Bonus and approved minutes also extend the weekly limit. While allowed
  hours, bedtime or a pause apply, the tray offers no time requests and
  approving one returns an error to the parent; open requests expire at
  midnight.
- "Bedtime +N" after bedtime (also after midnight) extends from now; with a
  shared budget it adds to the longest extension any PC granted.
- A user whose time status cannot be worked out is enforced from the last
  known status, or is out of time after three ticks.
- Rules and focus apply to every seat of a user, not only the main one.
- Yes/no settings given as text mean what they say (`unlimited: "false"` was
  true); anything else is a configuration error.
- New checks: `activity.idle_seconds` / `away_seconds` at least 10 (away not
  below idle), `history_days` at most 60, `storage.flush_seconds` at most 300,
  `logging.max_bytes` at least 100000, no negative grace or break settings;
  `24:00` can no longer start a window; rules whose Home Assistant entities
  would get the same id ("YouTube" and "YouTube weekly") are refused.
- `pclink.set_limit` / `pclink.rule_limit` accept `minutes: unlimited`; an empty
  value goes back to the configured limit (the texts said "unlimited").
- Actions sent to several PCs return the results of the PCs that took them
  alongside per-PC errors; `target:` accepts floors and labels.
- Home Assistant: entities stay unavailable after a reconnect until fresh
  state arrives; a throttled (429) or busy (503) PC is retried after its
  `Retry-After`, reconnects have jitter and pause while a new token is asked
  for.
- mDNS announces addresses that change, leaves out virtual and VPN adapters
  and no longer publishes the version.
- One screenshot at a time, at most 3840 pixels wide (`max_width: 0` too).
- Hardware sensors with the same name are told apart by their
  LibreHardwareMonitor identifier (their entity ids change).
- Site rules, block lists and focus allow-lists also apply to installed web
  apps and `--app` windows of Chrome, Edge, Brave and Firefox (the address is
  read from the page itself). During focus, a browser window whose site cannot
  be read is closed like a tab once it has stayed unknown for 5 seconds - also
  when the browser is in `allow_apps`; `focus.allow_unknown_sites` allows such
  windows. New activity attributes `site_unknown` and `app_id`.
- A grant made while allowed hours, bedtime or a pause keep the user off
  (bonus, earned minutes, a higher limit, a profile) no longer restarts the
  grace period; the minutes count once that no longer applies.
- Home Assistant: when a PC asks it to wait (too many wrong tokens), commands
  and polls to that PC stop until the time is up, and actions fail at once
  with "asked to wait N s"; action responses carry `retry_after` per PC.

### Upgrade notes

- PCs that used MQTT: run the installer (or `pclink.exe setup-api`), add the PC with
  the PC Link integration and delete the old MQTT device in Home Assistant.
  Entity ids follow the same pattern, but names, areas and long-term
  statistics of the MQTT entities do not carry over. The `mqtt:` section can
  be deleted from `config.yaml`.
- Put the parent PIN in quotes. An unquoted PIN that YAML alters (a leading
  zero, for example) now stops the configuration from loading.
- Set `api.allow_config_edit: true`, or an `api.admin_token`, if the setup
  page should still edit the configuration.
- Add `focus` to a custom `pin_required_for` if starting focus should need
  the PIN (it is in the default list).
- Users whose names have no ASCII form, and users whose ids collided, get a
  new Home Assistant id.
- The updater refuses releases without a `VERSION` file (every release built
  by this version's workflow has one).
- For signed builds, move the certificate secrets into the GitHub environment
  `release-signing`.
- With a shared budget, the *bonus today* and *bedtime extension* numbers
  show what this PC granted.
- A PC moved to another time zone keeps the zone it was pinned to: set
  `time_limits.time_zone` there.
- A bedtime without `wake_time` now ends the night at 06:00 (with a warning)
  instead of at midnight; set `wake_time` if you want another hour.
- Run `check-config` after updating: settings that used to be accepted (see
  *Changed*) can now stop the configuration from loading.
- Agents of the previous version are refused by the new service and replaced
  by it automatically.

### Fixed

- **ASTER multiseat:** only the first seat was reported. ASTER lists every
  further workplace to the service as a *disconnected* session named
  "Multiseat", so those users got no agent, showed as disconnected with
  unknown app, website, volume and display, and their screen time was neither
  counted nor limited. Such a session is now taken as the active seat it is;
  whether it is locked comes from its agent. Agents are also told how many
  other seats are in use, since inside an ASTER workplace Windows lists none,
  so one seat's game controller no longer keeps another seat "active".
- PC sensors froze at their first value for the Home Assistant integration.
- Agents of sessions with an unchanging status reconnected every 95 seconds.
- A configuration error during a hot reload could make ticks fail; wrong value
  types are now reported by `check-config`.
- Allowed hours that wrap past midnight (`fri: "16:00-01:00"`) now continue
  into the next day's morning.
- A bedtime extension granted after midnight applies to the running night.
- Warnings are shown again after Explorer restarts (tray icon is re-created).
- Commands that did nothing (wrong PIN, disabled power action, unknown or empty
  user) now report an error instead of success.
- `daily_limit_minutes: off` was read as 0 minutes; it now means no limit.
- Site rules written as `www.youtube.com`, `https://...` or a single host
  such as `docs.google.com` never matched; international domain names and IP
  addresses could not be limited.
- A night without a bedtime ended at midnight when the next day had one.
- Ending focus during a scheduled window cancelled the rest of the day's
  windows.
- Setting a weekly limit to 0, or a limit to "no limit", from Home Assistant
  restored the configured value.
- A hung agent was never restarted, which switched app rules off for that
  seat; the agent now also restarts itself when its sampling hangs.
- The tray icon stayed grey after a reconnect.
- The web address was not updated for a new page with the same title, and
  could be taken from text being typed.
- Changing the PC's clock could stall publishing, agent restarts and the
  configuration reload.
- A configuration that could not be applied was left half-applied; removed
  rules, apps and users kept their entities.
- "Install updates" could freeze the service for two minutes; "top process"
  showed the idle process; telemetry, device and GPU readings delayed the
  main loop.
- GPU load on non-English Windows; an integrated GPU added to a graphics
  card; "restart required" triggered by pending file renames alone.
- Wake-on-LAN used the wrong adapter with virtual network adapters installed,
  and assumed a /24 network.
- A UAC prompt was reported as a locked session; two lock requests in a row
  disconnected the session.
- `logoff_only` wrote an event every few minutes; logon hours an account had
  before a block were lost after it.
- A corrupt database stopped the service; it is now moved aside and replaced.
- Screenshots too large to transfer dropped the agent's connection.
- The install scripts opened the firewall for the wrong port.
- Home Assistant: an empty catalog could remove every entity; roll-up totals
  dropped after a restart while a PC was off; a failed send to a PC was
  retried without pause; `parent_present` had no PIN field.
- A bedtime without `wake_time` ended at midnight; "00:30" locked the user out
  all day and "00:00" was never enforced.
- Suspending the agent during focus mode could put off the logoff forever.
- Switching limits back on logged off at once, without a grace period.
- Notepad, Paint, Terminal, Calculator, Photos and other built-in Windows apps
  counted as games, and full-screen media players as gaming.
- A rule `message` that is not text, or a malformed rule duration, stopped all
  of that user's rules.
- A rule with only allowed hours closed the program without a warning; focus
  schedules past midnight stopped at midnight.
- Break reminders never noticed a real break.
- Shared budgets: a negative bonus on another PC was ignored, and a huge
  number from one PC dropped the figures of the others.
- `activity.idle_seconds: 0` made everything idle, switching rules and focus
  off.
- A clock set far ahead made the daily purge delete real history; old days
  stayed in memory.
- One Windows session that could not be read hid every other session.
- A weekly summary that failed was not retried; an empty week raised an event.
- A time request approved after midnight credited the new day.
- On nights the clocks change, warnings came an hour off (now also on a PC
  whose time zone cannot be identified).
- Update signatures could not be read for files in a folder with non-ASCII
  characters in its name, which refused every update installed there.
- mDNS announced the address of an OpenVPN (TAP) or other VPN adapter with a
  generic name, which Home Assistant could pick instead of the LAN address.
- Junctions in the data folder were left out of the list of foreign files.
- Older Home Assistant versions (2024.3): removing the second-to-last PC left the
  "all PCs" device behind, and shared-budget usage could be sent late.
- A slow or stuck command (closing a program, a malformed request) blocked the
  agent's connection or went unanswered; a failing audio or presence reading
  lost the whole report; the first report after a reconnect waited 15 s.
- A drifting controller stick kept the seat active; turning the monitor on
  from Home Assistant made the seat look used.
- Reading the browser address could fall back to titles for good, and searched
  web content (turning on browsers' accessibility trees).
- Hardware sensors and disk usage delayed the service start and the main
  loop; the GPU counter query is rebuilt every 6 hours.
- `time_limits.enabled` was not applied on a configuration reload.
- Home Assistant: re-adding a PC over HTTP kept its old HTTPS settings; the
  "different PC answers" repair stayed after a Reconfigure; unloading could add
  entities to unloaded platforms; the roll-up counted minutes of another day;
  a refused shared-budget update was retried without pause; a message the PC
  refused was shown as set.

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
