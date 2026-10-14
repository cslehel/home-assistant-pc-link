# Home Assistant integration

The **PC Link integration** (a HACS custom integration with a UI setup flow)
connects a PC to Home Assistant through the PC's local API: WebSocket push
plus REST. The PC is found on the LAN automatically (mDNS).

---

## Setup

### 1. On the PC: the local API

**Standalone build:** nothing to do - the installer sets it up (the API on
for the LAN, a token, HTTPS with a certificate of the PC's own, the firewall
rule) and ends by showing the token. `pclink.exe setup-api --show` shows it
again.

**Python install, or to set it up by hand** (elevated prompt):

```powershell
python -m pclink setup-api
```

writes it all into `%ProgramData%\PCLink\config.yaml`, restarts the service
and prints the token. It keeps a token and a certificate that are there
already, so it can be run again at any time; `--new-token` / `--new-cert`
replace them (Home Assistant then needs the new one), `--no-tls` leaves HTTPS
off. A Python install also needs the firewall rule:

```powershell
New-NetFirewallRule -DisplayName "PC Link API" -Direction Inbound -Protocol TCP -LocalPort 47392 -Action Allow -Profile Private
```

(`pclink api-token` and `pclink make-cert` still print the pieces for editing
the `api:` section by hand.)

The integration uses `api.token`. An optional second token, `api.read_token`,
can only read; setting it also stops the other users of the PC from reading
the API without a token (see [API.md](API.md#authentication)).

### 2. In Home Assistant: install the integration

**HACS:** HACS → Integrations → ⋮ → *Custom repositories* → add
`https://github.com/cslehel/home-assistant-pc-link` as *Integration* → install
**PC Link** → restart Home Assistant.

**Manual:** copy `custom_components/pclink/` from this repository into your
Home Assistant `config/custom_components/` folder and restart.

### 3. Add the PC

Settings → Devices & services. If the PC is on the same network it is already
listed as **Discovered** (the service announces itself with mDNS): click
*Configure*, enter the token, done. Otherwise *Add integration* → **PC Link**,
enter the PC's IP address or host name, the port and the token.

A device named after the PC appears with every entity; new users, rules,
apps or hardware sensors you add to `config.yaml` later show up by themselves
after the PC reloads its configuration (no restart of Home Assistant).
Entities of something you removed become unavailable at once and are deleted
after about 10 minutes.

Token changed on the PC? Home Assistant asks for the new one
(*Reconfigure* also lets you change the address; Home Assistant 2024.3 has no
such menu yet - there, add the PC again with its new address and the existing
entry follows it). The address may be pasted as the browser shows it
(`http://192.168.1.5:47392/`). The token field of *Reconfigure* may be left
empty to keep the stored token only while the address stays the same, or when
the PC has to show the certificate whose fingerprint is pinned; for any other
address the token has to be typed again, so that it never goes to an address
that has not proved to be the PC. The dialog shows how the PC is connected now
(the pinned fingerprint, HTTPS without one, or plain HTTP).

**Which token.** The integration needs a token that may send commands, so
`api.read_token` is refused. Enter one of these, when adding the PC and when
Home Assistant asks for a new token:

| Token | What Home Assistant can do with it |
|---|---|
| `api.token` (everyday) | everything on this page - limits, bonus minutes, focus, rules, profiles, lock / log off, power, messages, screenshots, time requests - **except** the things below when `api.admin_token` is set on the PC |
| `api.admin_token` | all of the above, plus: the custom-command buttons (`commands:` in `config.yaml`), `pclink.launch` with a free command line (a configured app by `name` works with the everyday token), *Reload configuration* / `pclink.reload_config`, and installing a PC Link update from the update entity |

Without `api.admin_token` on the PC there is only one token and it may do
all of it. With the everyday token entered, the admin things fail with
"this needs the PC's admin token (api.admin_token)" - nothing else is
affected and Home Assistant does not ask for a new token because of it - and
the update entity shows the new version without an *Install* button. To use
them from Home Assistant, enter the admin token instead (*Reconfigure*, or
add the PC again on Home Assistant 2024.3). Which one to use is a choice: the
everyday token keeps running custom commands and replacing the service out of
reach of whoever controls Home Assistant.

A PC that is already set up is never moved to a new address by a discovery
announcement (anything on the network can send one). When the PC cannot be
reached where it is configured and announces itself somewhere else, a
**repair** appears under Settings → System → Repairs with the new address
filled in: check it, enter the token again and submit. Only the address and
port come from the announcement - HTTPS and the pinned certificate stay as they
were - and the stored token is only reused when that pinned certificate is
kept (then only the real PC can answer). It goes away by itself when the PC
connects again. Giving the PC a fixed address avoids all of it.

The integration checks on every connection that the PC answering is the one
the entry was made for (its `device.id`). If the address now belongs to another
PC Link PC - two PCs with the same token that swapped addresses, say - that PC
is ignored: nothing is read from it, no command is sent to it and no entity is
removed; the entry's entities are unavailable and a repair names both PCs and
asks for the right address. Changing `device.id` on a PC has the same effect:
remove the entry and add the PC again.

**HTTPS.** With `api.tls_cert` / `api.tls_key` set on the PC (`setup-api`
does it; see [SECURITY.md](SECURITY.md)) the API is served over HTTPS. A
discovered PC brings the certificate's fingerprint along, so nothing more is
entered; the confirmation dialog shows that fingerprint (or "HTTP, not
encrypted") - compare it with what `setup-api` printed on the PC before
entering the token, since anything on the network can send an announcement.
When adding the PC by hand, switch *HTTPS* on and paste the SHA-256
*fingerprint* that `setup-api` printed; only that certificate is then
accepted, which is what a self-signed one needs. A PC that was added over HTTP
and switched to HTTPS later needs *Reconfigure* (HTTPS on, the fingerprint):
an announcement never changes a configured PC. Without a fingerprint the certificate is
checked the usual way (it must be issued for the host name by an authority
Home Assistant trusts).

### Actions

Every command is an action with a device picker (`device_id` may be omitted
when only one PC is configured) and an optional `pin` field for the parent PIN:

| Action | Fields |
|---|---|
| `pclink.add_minutes` | `user`, `minutes` |
| `pclink.earn` | `user`, `minutes`, `reason` |
| `pclink.reward` | `user`, `name` |
| `pclink.set_limit` | `user`, `scope` (today / weekday / weekend / weekly), `minutes` (empty = drop the override, the limit from `config.yaml` applies again; `unlimited` = no limit) |
| `pclink.clear_overrides` | `user` |
| `pclink.block` / `pclink.unblock` | `user` |
| `pclink.focus` / `pclink.focus_off` | `user`, `minutes`, `allow` |
| `pclink.extend_bedtime` | `user`, `minutes` |
| `pclink.rule_enabled` / `pclink.rule_limit` | `user`, `rule`, `enabled` / `minutes` (empty = back to the configured limit, `unlimited` = none), `scope` (today / weekly) |
| `pclink.set_rule` / `pclink.remove_rule` | `user`, `rule` and the rule's fields: see [Editing rules](#editing-rules-from-home-assistant) |
| `pclink.breaks` | `user`, `enabled` |
| `pclink.notify` | `message`, `title`, `user`, `level` |
| `pclink.logoff` / `pclink.lock` | `user` (optional) |
| `pclink.volume` | `user`, `level`, `muted` |
| `pclink.display` | `enabled` (on / off), `user` (optional: every seat) |
| `pclink.screenshot` | `user` |
| `pclink.launch` / `pclink.close_app` | `name` or `command` / `process`, `user` |
| `pclink.power` | `action` (shutdown / reboot / sleep / hibernate / cancel_shutdown), `delay`, `message` |
| `pclink.wake_on_lan` | `mac`, `broadcast`, `port` |
| `pclink.pin` / `pclink.pin_lock` | `pin` |
| `pclink.parent_present` | `present` |
| `pclink.block_item` | `user`, `item` (program or website) |
| `pclink.unblock_item` | `user`, `item` (or `all`) |
| `pclink.usage_report` | `user`, `hours` (returns the report) |
| `pclink.answer_time_request` | `user`, `approve` (on / off), `minutes` (optional: other than what was asked for): see [Asking for more time](#asking-for-more-time) |
| `pclink.set_profile` | `profile` (a configured name, or `normal`), `user` (optional: everyone who has it): see [Profiles](#profiles-holiday-school-day-) |
| `pclink.reload_config` | |
| `pclink.command` | `data`: any JSON command from the table at the end of this page (returns the PC's response) |

```yaml
# Grant Kevin 20 more minutes (PIN inline when one is configured)
action: pclink.add_minutes
data:
  user: kevin
  minutes: 20
  pin: "1234"
```

To remove a limit for good, use `minutes: unlimited` (YAML; the number field
of the action editor cannot hold it). Leaving `minutes` out only drops the
override made from Home Assistant, after which the limit in the PC's
`config.yaml` applies again; `pclink.clear_overrides` does that for all of a
user's limits at once.

A refused action (for example "parent pin required") fails with that message,
so it is visible in the automation trace or the UI.

`pclink.command`, `pclink.launch` and `pclink.reload_config` run programs on
the PC or change what it runs, so only Home Assistant **administrators** may
call them from the UI; automations and scripts can still use them. The other
actions are open to every user as before (anything that gives time is guarded
by the parent PIN on the PC).

Instead of `device_id` an action also takes a `target:` with devices, with
entities of a PC, with an area, a floor or a label (floors and labels need
Home Assistant 2024.4 or later). With several PCs targeted, every PC is tried;
when one fails the action fails with a message naming the PCs that refused or
could not be reached. Called with a response (`response_variable:`), it only
fails when all of them did: the response then holds `results` from the PCs
that took it and `errors` (PC id → message) for the others.
The **PC Link (all PCs)** device is a target
too: the action then goes to every PC that is loaded. That works for the actions
that mean the same on every PC - `block` / `unblock`, `focus` / `focus_off`,
`extend_bedtime`, `breaks`, `rule_enabled`, `set_rule` / `remove_rule`,
`block_item` / `unblock_item`, `notify`, `logoff`, `lock`, `display`, `volume`,
`close_app`, `power`, `pin` / `pin_lock`, `parent_present`, `usage_report`,
`reload_config`. The others do one thing on one PC (minutes granted on every PC
would be granted several times) and are refused for it.

After a switch, number, select or text was set, the entity shows the new value
at once and the PC's actual state as soon as it answers (the integration asks
for it right away instead of waiting for the next push).

When the PC asks Home Assistant to wait - it refuses an address for a while
after too many wrong tokens (HTTP 429 with `Retry-After`, or a refusal with
`retry_after` in its answer) - the integration does as asked: until that time
is up nothing is sent to that PC and it is not polled. An action or entity
command in that time fails at once, without being sent, with the error "*PC*
asked to wait *N* s before it takes commands again"; called with a response,
the response has `retry_after` (PC id → seconds) next to `errors`.

### Events

Every event the PC reports is fired on the bus as `pclink_event` with
`device_id`, `pc_id`, `pc_name`, `event` and, for user events, `user`
(plus event-specific fields such as `minutes` or `reward`). The **device
triggers** below are built on these.

Events are not lost while Home Assistant restarts or the connection is down:
after every reconnect the integration asks the PC for the events it recorded
meanwhile and fires the ones it has not seen, oldest first, with
`replayed: true` in the event data (not older than a day, at most 200; this is
also how the PC's own "service started" arrives, which always happens before
the connection is up). An automation that must only react to things happening
right now can test for `replayed`.

### Several PCs

Add each PC as its own integration entry. From the second PC on, the
integration adds the device [**PC Link (all PCs)**](#the-all-pcs-device) with
every user's totals across the PCs. With `time_limits.shared_budget: true`
on a PC, the integration also tells that PC what its users did on the other
PCs, so one limit spans all of them
(see [Shared budgets](#shared-budgets-across-pcs)).
The minutes a PC reported are remembered, so a PC that is switched off still
counts in today's totals after Home Assistant restarts. When only one PC is
left the device and its entities are removed again.

### Diagnostics

*Download diagnostics* on the integration entry gives a file meant to be
attached to an issue: it keeps the structure, numbers, states and dates of
what the PC reports and replaces every other text - names, addresses,
programs, websites, messages - by `**REDACTED**`.

### Privacy: repairs, the recorder and the PIN

- The two repairs ("announced itself at a new address", "a different PC
  answers") name the PC and the addresses involved (and the other PC's name).
  They are kept by Home Assistant until fixed and appear in Home Assistant's
  own diagnostics and repair lists, so mind that before sharing those.
- Attributes that change with every push (top processes, today's apps, sites
  and games, the history, presence and activity details, sessions, media) are
  not written to the recorder; the PC says which ones (`unrecorded_attributes`
  in its entity catalog), an older PC gets a built-in list. Their current
  values are still there for templates and dashboards.
- A parent PIN typed into the **Parent PIN** text entity, or given as `pin`
  to an action, travels in Home Assistant's `call_service` event, and the
  recorder stores events. The entity itself never shows the PIN. To keep it
  out of the database, exclude the entity and the event:

  ```yaml
  recorder:
    exclude:
      entities:
        - text.family_pc_parent_pin
      event_types:
        - call_service
  ```

  (Excluding `call_service` drops every action call from the logbook's
  history; keep the PIN in `secrets.yaml` - `pin: !secret pclink_pin` - for
  automations either way.)

---

## Entities

Everything is grouped under one HA **device** named after the PC. Its entities:

## PC entities
- `sensor` CPU usage, CPU frequency, memory usage/used, swap, per-disk usage &
  free, disk read/write, network up/down, GPU load/memory/temp/power, battery,
  last boot, uptime, processes, IP, MAC, logged-in users, active sessions.
- `sensor` **Top process (CPU / memory)** with the top-N list as attributes
  (`telemetry.top_processes: N`).
- `sensor` **USB devices** (list as attribute), **Removable drives**,
  **Printer** (per-printer states as attributes), **Print jobs**
  (`telemetry.usb_devices`, `telemetry.printers`).
- `sensor` **Windows updates pending** (titles as attributes), `binary_sensor`
  **Reboot pending**, `sensor` last update check; `button` **Check for
  updates** and, with `allow_install`, **Install updates** (`windows_update:`).
- `binary_sensor` someone using the PC,
  **someone at the PC** (presence), battery charging, **Parent PIN unlocked**.
- `text` **Parent PIN** (password field; see *Parent PIN* below), broadcast
  message to everyone.
- `switch` time limits (global on/off), **Parent at home** (auto-extends
  bedtimes; set it from a presence automation).
- `button` shut down, reboot, sleep, hibernate, cancel shutdown, lock all
  sessions, **Reload configuration**, **Launch/Close <app>** for each entry in
  `apps:`, plus any custom commands.
- Hardware sensors (temps/fans/power) appear as individual `sensor` entities.
- `update` **PC Link update**: the version installed on the PC and the latest
  release on GitHub, with an *Install* button (standalone build, unless
  `updates.allow_install: false`), plus a **Check for PC Link update** button.
  The PC checks every `updates.check_interval_hours` and downloads the release
  itself; the download must match the SHA-256 GitHub publishes for it, and a
  code-signed copy only accepts a release signed by the same publisher (see
  [SECURITY.md](SECURITY.md#code-signature-of-an-update)). When an update
  fails or is refused, the entity's attributes say why: `status` (`error`),
  `error` (the reason) and `signature` (what the signature check found), next
  to `last_check`, `can_install`, `auto_install` and `repository`. Set
  `updates.auto_install: true` to install new releases unattended. The service
  restarts during the update, so the PC is briefly unavailable. A private
  repository needs `updates.github_token`.
- `button` **Wake up**: Home Assistant sends a Wake-on-LAN
  packet to the PC's network card. The integration remembers the PC's MAC
  address and entities, so the button also works after Home Assistant was
  restarted while the PC is off. Wake-on-LAN must be enabled in the PC's
  BIOS/UEFI and network adapter settings, and Home Assistant must be on the
  same network (in a container: host networking).

## Per-user entities (each managed user)
- `sensor` status, **presence** (`active` / `passive` / `idle` / `away`, with
  the reason and raw signals as attributes — see [PRESENCE.md](PRESENCE.md)),
  **idle time today**, **away time today**, **usage in the last 10 h**
  (applications, games, websites and presence minutes as attributes),
  **blocked apps and websites** (the block list),
  activity, current app, current website, **now playing**, time used today,
  gaming today, browsing today, **time used this week**, time remaining,
  daily limit, **weekly time remaining**, **rolled-over minutes**,
  **earned today**, **bedtime tonight**, **focus time remaining**,
  **continuous screen time**, and one sensor per rule (`ok` / `used_up` /
  `blocked` / `outside_hours` / `off`, details as attributes) plus a
  `<rule> remaining` sensor for rules with a limit and a
  `<rule> remaining this week` sensor for rules with a weekly budget.
- `binary_sensor` logged in, login blocked, within allowed hours,
  **past bedtime**.
- `number` limit today, weekday limit, weekend limit, **weekly limit**, bonus
  today, **add minutes** (adds and resets to 0), **bedtime extension**,
  **focus duration**, **volume**, `<rule> limit today`,
  `<rule> weekly limit` (0 = none).
- `button` +15 min, +30 min, one **reward** button per `rewards:` entry,
  **bedtime +30 min**, **take screenshot** (when `screenshots.enabled`), log
  off, lock.
- `switch` limit enabled, access paused, **focus mode**, **break reminders**,
  **muted**, **display** (turns that seat's monitor off and on; needs the
  agent, `agent.display_control`), `<rule> rule` (enable/disable a rule).
- `camera` **screenshot** — the last screenshot taken (when enabled).
- `text` message to this user, **block app or website**, **unblock app or
  website**.

Unlimited users (e.g. parents) get the statistics entities (including
presence, idle / away time, the usage report, now playing, volume and
display) but no limit controls and no block list, and are never captured by the screenshot
feature.

## Device triggers

Every user and the PC get native **device triggers** (Settings → Automations →
*Device* → pick the PC): *"Kevin ran out of time"*, *"is about to run out of
time"*, *"entered the grace period"*, *"logged in / logged off"*, *"got login
blocked / unblocked"*, *"reached bedtime"*, *"started / ended focus mode"*,
*"is due for a break"*, *"hit an app or site limit"*, *"had an app or tab
closed"*, *"earned bonus minutes"*, *"had a screenshot taken"*, *"left the
seat"*, *"came back to the seat"*; for the PC: *"service started"*,
*"configuration reloaded"*, *"parent PIN entered"*, *"wrong parent PIN"*,
*"needs a reboot for updates"*, *"has Windows updates pending"*, *"USB
devices changed"*, *"printer reported an error"*.

Two more events are fired as `pclink_event`, without a device trigger:
`pclink_update_available` (a newer PC Link release was found) and
`blocklist_changed` (with `user`, `item` and `blocked`).

## Tampering and service health

Per managed user:

- **`<user> tampering suspected`** (binary sensor, device class *tamper*) turns
  on when something looks like an attempt to get around PC Link, and
  **`<user> tampering reason`** says what: `agent_silent` (the tray program
  stopped reporting), `agent_killed` (it was ended), `report_rejected` (it sent
  something that cannot be true), `clock_changed`, `time_zone_changed`. The
  reason's attributes hold the time and details. The device trigger
  *"… may have tampered with PC Link"* fires at that moment.
- **`<user> clear tampering warning`** (button) turns the warning off again
  once you have looked into it.

Per PC, under *Diagnostic*:

- **`Service problem`** (binary sensor, device class *problem*) is on while
  the service's own checks keep failing - time may then not be counted or
  enforced properly. Device trigger: *"Service is not working properly"*.
- **`Service errors`** (how many checks failed since the service started; the
  attributes carry the last error) and **`Last complete check`** (a time
  stamp) are disabled by default; enable them when you look into a problem.

```yaml
# Tell a parent when a child's PC Link agent was ended
automation:
  - alias: "PC Link: tampering"
    trigger:
      - platform: state
        entity_id: binary_sensor.family_pc_kevin_tampering_suspected
        to: "on"
    action:
      - action: notify.mobile_app_dads_phone
        data:
          title: "PC Link"
          message: "Kevin's PC: {{ states('sensor.family_pc_kevin_tampering_reason') }}"
```

## Profiles (holiday, school day ...)

With `time_limits.profiles` in the PC's `config.yaml` (see the example
config) every managed user who has profiles gets a **`<user> profile`**
select: `normal` plus the configured names. Choosing one applies that
profile's limits, allowed hours and bedtime until another is chosen; what you
set by hand (limit today, weekday limit ...) still wins. A profile that gives
more time needs the parent PIN, like any grant. The list follows the PC's
configuration: a profile added there appears after the PC reloads it.

`pclink.set_profile` does the same from an automation - with `user` for one
user, without for everyone who has that profile, and with **PC Link (all
PCs)** as the device for every PC:

```yaml
# Holiday limits on every PC during the school holidays
automation:
  - alias: "PC Link: holiday profile"
    trigger:
      - platform: state
        entity_id: calendar.school_holidays
    action:
      - action: pclink.set_profile
        target:
          device_id: <id of "PC Link (all PCs)">
        data:
          profile: "{{ 'holiday' if trigger.to_state.state == 'on' else 'normal' }}"
          pin: !secret pclink_pin
```

The event `profile_changed` (with a device trigger) reports every switch.

## Asking for more time

With `time_limits.time_requests` enabled on the PC (it is by default) the tray
menu of a managed user offers *Ask for more time* with the configured amounts.
A request shows up as **`<user> time request`** (the minutes asked for, 0 when
there is none; the attributes say when it was made and when it expires) and
can be answered with the **`<user> approve time request`** /
**`<user> deny time request`** buttons or with `pclink.answer_time_request`
(`user`, `approve`, optionally other `minutes` than asked for, `pin`). The
child is told either way. Approving is a grant: with a parent PIN configured
it needs the PIN or an unlocked PIN. Events / device triggers:
`time_requested`, `time_request_approved`, `time_request_denied`,
`time_request_expired`.

The approved minutes are bonus minutes for the day, and they count against the
weekly limit too. While the allowed hours, bedtime or a pause keep the child
off the PC, more minutes would change nothing: the tray offers no amounts
then, and approving a request made earlier fails with an error that says why
(the request stays open - deny it, or change the allowed hours / extend
bedtime instead). A request is for the day it was made on: one still open at
midnight expires.

An actionable notification on a parent's phone (Home Assistant companion app):

```yaml
automation:
  - alias: "PC Link: Kevin asks for more time"
    mode: parallel
    trigger:
      - platform: event
        event_type: pclink_event
        event_data:
          event: time_requested
          user: kevin
    # a request made while Home Assistant was restarting arrives later, marked
    # as replayed: still worth asking, it expires on the PC by itself
    action:
      - variables:
          minutes: "{{ trigger.event.data.minutes }}"
          pc: "{{ trigger.event.data.device_id }}"
          tag: "pclink_{{ context.id }}"
      - action: notify.mobile_app_dads_phone
        data:
          title: "{{ trigger.event.data.pc_name }}"
          message: "Kevin asks for {{ minutes }} more minutes."
          data:
            tag: "{{ tag }}"
            actions:
              - action: "APPROVE_{{ context.id }}"
                title: "Give {{ minutes }} min"
              - action: "HALF_{{ context.id }}"
                title: "Give {{ (minutes | int / 2) | round | int }} min"
              - action: "DENY_{{ context.id }}"
                title: "No"
      - wait_for_trigger:
          - platform: event
            event_type: mobile_app_notification_action
            event_data:
              action: "APPROVE_{{ context.id }}"
          - platform: event
            event_type: mobile_app_notification_action
            event_data:
              action: "HALF_{{ context.id }}"
          - platform: event
            event_type: mobile_app_notification_action
            event_data:
              action: "DENY_{{ context.id }}"
        timeout: "00:30:00"          # time_requests.expire_minutes on the PC
        continue_on_timeout: false
      - action: pclink.answer_time_request
        data:
          device_id: "{{ pc }}"      # the PC the request was made on
          user: kevin
          approve: "{{ not wait.trigger.event.data.action.startswith('DENY_') }}"
          minutes: >-
            {{ (minutes | int / 2) | round | int if wait.trigger.event.data.action.startswith('HALF_') else minutes | int }}
          pin: !secret pclink_pin    # only when a parent PIN is configured
```

`pclink.answer_time_request` always goes to one PC (the request waits on the
PC it was made on), so it is refused for the "all PCs" device.

## Upgrade notes

- The integration now checks that the PC answering at an entry's address is
  the PC that was added. If you changed `device.id` on a PC after adding it,
  the entry reports "a different PC answers": remove it and add the PC again.
- `pclink_event` is also fired for events the PC recorded while Home Assistant
  was not connected, with `replayed: true`. Automations that trigger on the
  event directly (not through a device trigger's own conditions) and must not
  act on something that happened minutes ago should add the condition
  `{{ not trigger.event.data.get('replayed', false) }}`. Device triggers fire
  for replayed events as well.
- The roll-up's `total time today` sensors changed from *total increasing* to
  *total* with a daily reset, so that a correction is not counted as a new
  day. Home Assistant may show a one-time notice under Developer tools →
  Statistics for them; choose to keep the statistics.
- `<user> login blocked` no longer has the *lock* device class (it showed
  "Unlocked" while the login was blocked). It reads on / off now; dashboards
  that relied on the lock icon need a look.
- With `api.admin_token` set on the PC, the token Home Assistant holds
  (`api.token`) no longer runs custom commands, reloads the configuration or
  installs updates; see "Which token" above.
- The "Visit" link on the PC's device page opens the PC's setup page (over
  HTTPS when the entry uses it); the page asks for the token itself.
- With one PC left, the "PC Link (all PCs)" device and its entities are
  removed. They come back when a second PC is added.
- Diagnostics redact every free text; attach them to issues as they are.

## Parent PIN

With `time_limits.parent_pin` (or `parent_pin_sha256`) set, Home Assistant can
only **loosen** a limit after the PIN was entered in the **Parent PIN** text
entity (valid for `pin_timeout_seconds`, default 5 minutes) or passed as
`"pin"` in a JSON command. Loosening means granting or earning minutes,
raising or removing a limit (today, weekday, weekend, weekly, per rule),
pausing a limit, un-pausing access, starting focus mode with your own
allow-list, ending focus mode early, extending bedtime, taking screenshots and
removing an entry from the block list. Tightening (pausing access, lowering
limits, starting focus mode with the configured allow-list, blocking an app or
website, logging off) never needs it, and neither does sending a value that is
already set. Add `parent_present` to `time_limits.pin_required_for` if the
**Parent at home** switch should need the PIN as well (leave it out when an
automation sets the switch). Wrong attempts are rate limited - the lockout
survives a restart of the PC - and raise the *"wrong parent PIN"* trigger; a
wrong PIN typed into the entity is reported as an error. Refused actions are
shown in `time remaining` → `limit_details` → `last_refusal`.

## The "all PCs" device

A virtual device **PC Link (all PCs)**: *PCs online*, *PCs in use*, *Any PC
in use* and, per user, *total time today* (minutes per PC, the tightest
remaining time and more as attributes), *status (all PCs)*, *active on* and
*logged in anywhere*.

It appears as soon as two PCs are set up and is worked out in Home Assistant
from the state of each PC; nothing has to be configured. A PC that is
switched off still counts with the minutes it reported today. The entities
belong to one of the PC entries and move to another one when that PC is
removed.

## Shared budgets across PCs

Set `time_limits.shared_budget: true` on every PC to make each user's daily
and weekly limits span all of them: the minutes a user spent on the other PCs
count on this one too (shown as `other_pcs_today_minutes` in the *time used
this week* sensor's attributes). The user names must be the same on the PCs.

Home Assistant passes every PC with a shared budget the usage of the other
PCs - when it changes, at most every 30 seconds, and again whenever the PC
reconnects. Nothing has to be set up beyond `shared_budget: true` (and
`fleet.enabled`, the default). A PC keeps the figures it was last given: while
Home Assistant is down or another PC is off, time already used elsewhere today
still counts, but new usage on the other PCs is not seen until Home Assistant
is back.

## Attributes worth using
- `time used today` carries a **7-day history** attribute (for a chart).
- `gaming today` / `browsing today` carry **top games** / **top sites** maps.
- `time remaining` carries a `limit_details` attribute (schedule, allowed
  hours, bedtime schedule, warn thresholds, action, rewards, last refusal).
- `presence` carries the reason and every raw signal (input idle, controller,
  audio, display state, Windows presence, ...).
- `status` carries the list of that user's sessions (seat, state, idle, lock,
  presence).
- rule sensors carry used / limit / remaining minutes (today and this week),
  whether the rule is active right now, its full `definition` (programs,
  websites, limits per day, allowed hours) and where it comes from (`source`).

## Free-form JSON commands

Anything not exposed as an entity can be sent as JSON: through the
`pclink.command` action, or with `POST /api/command` (see [API.md](API.md)):

```yaml
# Grant Kevin 20 more minutes today (PIN inline when one is configured)
action: pclink.command
data:
  data: {"action": "add_minutes", "user": "kevin", "minutes": 20, "pin": "1234"}
```

Supported actions:

| Action | Fields | Effect |
|---|---|---|
| `shutdown`, `reboot`, `sleep`, `hibernate`, `cancel_shutdown` | `delay`, `message` | power |
| `logoff`, `lock`, `notify` | `user`, `session`, `title`, `message`, `level` | sessions |
| `wol` | `mac`, `broadcast`, `port` | wake another PC |
| `add_minutes`, `set_bonus` | `user`, `minutes` | bonus minutes (they count against the weekly limit as well) |
| `earn` | `user`, `minutes`, `reason` | bonus minutes recorded as earned |
| `reward` | `user`, `name` | claim a configured reward |
| `set_limit` | `user`, `minutes`, `scope: today\|weekday\|weekend\|weekly` | limits |
| `clear_overrides` | `user` | back to the configured limits |
| `block`, `unblock`, `limit_enabled` | `user`, `enabled` | pause / enable |
| `limits` | `enabled` | global on/off |
| `focus`, `focus_off` | `user`, `minutes`, `allow: [...]` | focus mode |
| `extend_bedtime`, `bedtime_extension` | `user`, `minutes` | bedtime (`extend_bedtime` after bedtime - also after midnight - extends from now) |
| `rule_enabled`, `rule_limit` | `user`, `rule`, `enabled` / `minutes`, `scope: today\|weekly` | per-app rules |
| `rule_set`, `rule_remove` | `user`, `rule`, the rule's fields (`rule_action` for its `action`) | create, change, remove a rule |
| `breaks` | `user`, `enabled` | break reminders |
| `volume`, `mute` | `user`, `level` / `muted` | audio in the user's session |
| `display` | `on`, `user` | turn the seat's display off / on |
| `screenshot` | `user` | screenshot to the camera entity |
| `launch`, `close_app` | `name` or `command` / `process`, `user` | apps |
| `parent_present` | `present` | same as the switch |
| `pin`, `pin_lock` | `pin` | unlock / re-lock the parent PIN |
| `check_updates`, `install_updates` | | Windows Update |
| `pclink_update_check`, `pclink_update_install` | | look for / install a new PC Link release |
| `block_item`, `unblock_item` | `user`, `item` | the per-user block list (`unblock_item` accepts `all`) |
| `usage_report` | `user`, `hours` | apps, websites and presence in the last hours (returned as `report`) |
| `run` | `name` | custom command |
| `reload`, `refresh` | | reload config / resend the full state |
| `set` | `entity`, `value` | any entity command by its key, e.g. `{"action":"set","entity":"user_kevin_blocked","value":"ON"}` |

Leave `user` out to address every session where a command allows it (`logoff`,
`lock`, `notify`, `volume`, `display`, `launch`, `close_app`); an empty `user` is refused.
Commands that could not do anything answer with an error (see
[API.md](API.md#commands)).

## Editing rules from Home Assistant

The per-app / per-site rules of `config.yaml` can be changed, and new ones
made, without touching the file:

```yaml
# A new rule: Roblox for 30 minutes on school days, an hour at the weekend, afternoons only
action: pclink.set_rule
data:
  user: kevin
  rule: Roblox
  apps: [RobloxPlayerBeta.exe]
  sites: [roblox.com]
  daily_limit_minutes: {weekdays: 30, weekend: 60}
  weekly_limit_minutes: 240
  allowed_hours: "16:00-19:00"
```

```yaml
# Change one thing of an existing rule (also of a rule from config.yaml)
action: pclink.set_rule
data:
  user: kevin
  rule: YouTube
  allowed_hours: {weekdays: "17:00-19:00", weekend: "10:00-20:00"}
  pin: "1234"        # only needed when the change allows more than before
```

- The fields are the ones a rule has in `config.yaml`: `apps`, `sites`,
  `categories`, `daily_limit_minutes`, `weekly_limit_minutes`,
  `allowed_hours`, `blocked`, `message` and `rule_action` (`close` or `warn`;
  the rule's `action`). Only the fields you give change. To take a limit
  away, give the text `unlimited`; for the hours, `all`.
- A rule whose name is not known yet is created; it gets its sensor, switch
  and numbers like a configured rule (the integration picks them up within a
  few seconds).
- **Parent PIN:** making a rule and tightening one are free. A change that
  allows more - more minutes on any day or per week, longer hours, fewer
  programs or websites covered, a warning instead of closing, no longer
  blocked - needs the PIN, and so does removing a rule.
- `pclink.remove_rule` deletes a rule made this way. For a rule from
  `config.yaml` it drops the changes made here and the configured rule applies
  again. A changed rule keeps its changed form when `config.yaml` is edited
  later; remove the change to follow the file again.
- Rules made here belong to one user on one PC and are stored in the PC's
  database, next to the block list.

`pclink.rule_limit` with `scope: weekly` (or the `<rule> weekly limit`
number) sets only the weekly budget of a rule; without `minutes` the
configured budget applies again.

## Blocking applications and websites from Home Assistant

Every managed user has two text entities, **block app or website** and
**unblock app or website**, and a sensor **blocked apps and websites** (the
number of entries; the attributes `apps` and `sites` list them). Type a program
name (`roblox` or `roblox.exe`) or a website (`youtube.com`, or paste its
address) into the *block* field: from then on the program is closed, or the
browser tab showing that site, whenever the user brings it to the front, with a
short notice. Typing an entry into *unblock* removes it (`all` clears the
list). The actions `pclink.block_item` and `pclink.unblock_item` (`user`,
`item`) do the same from automations.

Blocking needs no PIN; unblocking counts as "disable" for the
[parent PIN](#parent-pin). The list is stored on the PC and survives restarts;
rules written in `config.yaml` are separate and stay as they are. This is the
same mechanism as those rules, not a network filter: it needs the agent
running in the user's session, matches programs by file name and websites by
the address the browser shows (subdomains included), and applies to managed
users only. Parts of Windows and PC Link itself cannot be blocked.

## Example: usage report (applications and websites in the last 10 hours)

Every user has a sensor **usage in the last 10 h**. Its state is the minutes
spent in applications in that window; its attributes `apps`, `games` and
`sites` list each name with its minutes, longest first (`from` / `to` give the
window). Usage is recorded per clock hour, so the window starts at the full
hour ten hours ago. Change the length with `activity.report_hours` on the PC.

A Markdown card that shows it (replace the entity id with your user's sensor):

```yaml
type: markdown
title: Usage in the last 10 hours
content: |
  {% set e = 'sensor.lehel_pc_lehel_usage_in_the_last_10_h' %}
  **Applications** - {{ states(e) }} min since {{ (state_attr(e, 'from') or '')[11:] }}
  {% for name, minutes in (state_attr(e, 'apps') or {}).items() %}
  - {{ name }}: {{ minutes }} min
  {% endfor %}
  {% for name, minutes in (state_attr(e, 'games') or {}).items() %}
  - {{ name }} (game): {{ minutes }} min
  {% endfor %}

  **Websites** - {{ state_attr(e, 'website_minutes') }} min
  {% for name, minutes in (state_attr(e, 'sites') or {}).items() %}
  - {{ name }}: {{ minutes }} min
  {% endfor %}

  **Presence** - active {{ state_attr(e, 'active_minutes') }} min,
  watching/listening {{ state_attr(e, 'passive_minutes') }} min,
  idle {{ state_attr(e, 'idle_minutes') }} min,
  away {{ state_attr(e, 'away_minutes') }} min,
  locked {{ state_attr(e, 'locked_minutes') }} min
```

The same presence figures since midnight are the sensors **idle time today**
and **away time today**. Idle means no input for `activity.idle_seconds`
(2 minutes by default) and away no input for `activity.away_seconds`
(10 minutes) with nothing playing; time in those states is not credited to the
application that happens to be in front.

For any other window, the action `pclink.usage_report` (`user`, `hours`)
returns the same report as response data, e.g. to send it as a notification:

```yaml
- action: pclink.usage_report
  data: {user: kevin, hours: 24}
  response_variable: usage
- action: notify.mobile_app_phone
  data:
    message: >
      {% set r = (usage.results.values() | first).report %}
      Apps: {{ r.apps | dictsort(false, 'value') | reverse | map('join', ' ') | list | join(', ') }}
```

`GET /api/users/<user>/report?hours=10` returns it over the local API.

## Example: notify when a child runs low

```yaml
automation:
  - alias: "Warn me when Kevin has 10 minutes left"
    trigger:
      - platform: numeric_state
        entity_id: sensor.kevin_time_remaining
        below: 10
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Kevin has {{ states('sensor.kevin_time_remaining') }} min of PC time left."
```

## Example: device trigger + parent presence

```yaml
automation:
  - alias: "Kevin ran out of time"
    trigger:
      - platform: device
        domain: pclink
        device_id: <the PC's device id>
        type: time_up
        subtype: kevin
    action:
      - service: notify.mobile_app_phone
        data: {message: "Kevin's PC time is over."}

  - alias: "Parent at home extends bedtime"
    trigger:
      - platform: state
        entity_id: person.dad
    action:
      - service: switch.turn_{{ 'on' if trigger.to_state.state == 'home' else 'off' }}
        target: {entity_id: switch.family_pc_parent_at_home}
```

## Example: bonus-time button on a dashboard

```yaml
type: entities
title: Kevin's PC time
entities:
  - entity: sensor.kevin_time_remaining
  - entity: sensor.kevin_time_used_today
  - entity: sensor.kevin_presence
  - entity: number.kevin_limit_today
  - entity: button.kevin_15_min
  - entity: button.kevin_reward_homework_done_30_min
  - entity: switch.kevin_focus_mode
  - entity: switch.kevin_access_paused
  - entity: sensor.kevin_activity
  - entity: sensor.kevin_youtube
```

## Example: multi-PC roll-up dashboard

```yaml
type: entities
title: Screen time (all PCs)
entities:
  - entity: sensor.pc_link_all_pcs_pcs_online
  - entity: sensor.pc_link_all_pcs_pcs_in_use
  - entity: sensor.pc_link_all_pcs_kevin_total_time_today
  - entity: sensor.pc_link_all_pcs_kevin_active_on
  - entity: sensor.pc_link_all_pcs_sarah_total_time_today
  - entity: sensor.pc_link_all_pcs_sarah_active_on
```

## Example: WOL another PC through this one

```yaml
action: pclink.wake_on_lan
data:
  mac: "AA:BB:CC:DD:EE:FF"
```

## What happens when the connection to Home Assistant drops

**Time limits keep working.** Every decision (counting time, warnings, log
off / lock, login blocking, allowed hours, bonus minutes you granted earlier)
is made locally by the service and stored in its own SQLite database. Home
Assistant is a remote control and a display, never a dependency. A child gets
neither extra time nor a lock-out because the network or Home
Assistant is down. The per-user tray icons keep working too (they talk to the
service over localhost).

**Home Assistant shows the PC as unavailable** rather than showing stale
values: state is pushed over a WebSocket every few seconds. If the socket
drops, or the PC stops pushing for about two minutes, the integration switches
to polling and marks the entities unavailable until it can reach the PC again;
it then reconnects with a backoff of 1 s to 60 s.

**On reconnect everything resyncs by itself.** The integration reads the
entity catalog and the full current state, so entities come back with the
truth (used minutes, remaining minutes, blocked / paused switches). This also
happens when Home Assistant itself restarts. Statistics collected during the
outage are in the database and appear in the history attributes.

**Commands sent while the PC was unreachable are not replayed.** A button
pressed or a number changed during the outage is lost, deliberately: a
"shut down" or "log off" from three hours ago must never fire when the link
returns. The same goes for events: `time_up`, `logged_off` and similar device
triggers that happened during the outage are not delivered late, so
automations never react to stale moments. Check the entity state after the
PC is back and repeat the command if you still want it. Limits set through
the config file or applied before the outage are unaffected.
