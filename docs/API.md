# Local REST / WebSocket API and setup page

An optional HTTP server inside the service, next to (or instead of) MQTT. Off
by default. It is what the **PC Link Home Assistant integration** talks to
(see [HOMEASSISTANT.md](HOMEASSISTANT.md)); `python -m pclink api-token`
prints a ready-made block with a random token.

```yaml
api:
  enabled: true
  host: 0.0.0.0            # 127.0.0.1 = this PC only; anything else needs a token
  port: 47392
  token: change-me         # required for every write; required for everything off loopback
  allow_config_edit: true  # the setup page may save config.yaml
  advertise: true          # announce _pclink._tcp on the LAN (mDNS) so Home Assistant finds the PC
```

mDNS advertising needs the `zeroconf` package (in `requirements-windows.txt`)
and is skipped silently when the API is bound to loopback.

Open `http://127.0.0.1:47392/` on the PC for the **setup / status page**: users
with time left, presence and activity, rules and modes, quick actions (+15,
pause, focus, log off), recent events, PC facts and a config editor that
validates the YAML before saving and hot-reloads the service.

## Authentication

Send the token as `Authorization: Bearer <token>`, `X-Token: <token>` or
`?token=<token>`.

| Request | From loopback | From elsewhere |
|---|---|---|
| reads (`GET`) | no token needed | token |
| writes (`POST`, `PUT`) | token | token |

Without `api.token` the API is read-only. Binding to a non-loopback address
without a token is rejected at config validation. Grants still go through the
parent PIN when one is configured (`"pin"` field in the command).

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{ok, version, device, uptime_seconds, mqtt_connected, restart_needed}` |
| GET | `/api/status` | the PC payload (same JSON as the MQTT state topic) |
| GET | `/api/users` | `{user: payload}` for every known user |
| GET | `/api/users/<user>` | one user's payload (404 if unknown) |
| GET | `/api/users/<user>/screenshot` | the last screenshot as `image/jpeg` / `image/png` (token always required; 404 if none) |
| GET | `/api/entities` | the **entity catalog** (below) |
| GET | `/api/events?n=50&user=kevin` | recent events, newest first |
| POST | `/api/command` | run a JSON command (below); returns `{ok, error, ...}` |
| POST | `/api/reload` | hot-reload `config.yaml` |
| GET | `/api/config` | `config.yaml` as text |
| PUT | `/api/config` | validate, write and reload the YAML in the body |
| GET | `/api/ws` | WebSocket (below) |

### Entity catalog

`GET /api/entities` describes every Home Assistant entity this PC offers, in a
transport-neutral form (the same definitions drive MQTT discovery):

```json
{
  "device": {"identifiers": ["pclink_family_pc"], "name": "Family-PC", "model": "Windows PC", "sw_version": "0.3.0"},
  "version": "0.3.0",
  "users": {"kevin": {"name": "Kevin", "managed": true}, "dad": {"name": "dad", "managed": false}},
  "triggers": {"user": {"time_up": "ran out of time"}, "pc": {"started": "service started"}},
  "entities": [
    {"unique_id": "pclink_family_pc_cpu_percent", "key": "cpu_percent", "platform": "sensor", "name": "CPU usage",
     "value": "cpu_percent", "unit": "%", "state_class": "measurement", "icon": "mdi:cpu-64-bit", "enabled": true, "source": "pc"},
    {"unique_id": "pclink_family_pc_user_kevin_blocked", "key": "user_kevin_blocked", "platform": "switch",
     "name": "Kevin access paused", "value": "blocked", "command": "user_kevin_blocked", "source": "user", "user": "kevin"}
  ]
}
```

`platform` is the HA platform (sensor, binary_sensor, switch, number, button,
text, camera); `value` / `attributes` are dotted paths into the `pc` payload
(`source: pc`) or into `users[<user>]` (`source: user`); `command` is the key to
send with the `set` action. Optional fields: `unit`, `device_class`,
`state_class`, `icon`, `category` (config / diagnostic), `enabled`, `options`,
`precision`, `min` / `max` / `step` / `mode`, `password`.

The catalog changes when the configuration is reloaded or a new user logs in;
clients refresh it after the `config_reloaded` / `started` / `logged_in` events.

### Commands

The body of `POST /api/command` is the same JSON the MQTT `cmd` topic
accepts (see [HOMEASSISTANT.md](HOMEASSISTANT.md#free-form-json-commands)),
for example:

```json
{"action": "add_minutes", "user": "kevin", "minutes": 20, "pin": "1234"}
{"action": "focus", "user": "kevin", "minutes": 45, "allow": ["docs.google.com"]}
{"action": "rule_enabled", "user": "kevin", "rule": "YouTube", "enabled": false}
{"action": "screenshot", "user": "kevin"}
{"action": "launch", "name": "Minecraft", "user": "kevin"}
{"action": "set", "entity": "user_kevin_limit_today", "value": 120}
```

`set` runs any entity command by its catalog key with the value the entity
would receive (`ON` / `OFF`, `PRESS`, a number, text); it honours the parent
PIN like every other command (`"pin"` may be passed inline).

### WebSocket

Connect to `/api/ws` (with the token for a writable connection). The server
sends:

```json
{"type": "hello", "writable": true, "status": {...}}
{"type": "state", "pc": {...}, "users": {...}}      // every api.push_interval_seconds
{"type": "event", "ts": 1.7e9, "user": "kevin", "event": "time_up", ...}
```

and accepts:

```json
{"type": "ping", "id": 1}                            -> {"type": "pong", "id": 1}
{"type": "status", "id": 2}                          -> {"type": "status", "pc": ..., "users": ...}
{"type": "entities", "id": 4}                        -> {"type": "entities", "device": ..., "entities": [...]}
{"type": "command", "id": 3, "action": "block", "user": "kevin", "token": "..."}
                                                     -> {"type": "result", "id": 3, "ok": true}
```

`type` defaults to `command`; a `token` field authenticates a single command on
an otherwise read-only connection.

## Hot reload

Independently of the API, the service watches `config.yaml` (every 10 s) and
applies changes on the fly; Home Assistant discovery is republished so new
users, rules or apps appear immediately. The `Reload configuration` button
and the `reload` JSON action do the same. Changes to `mqtt`, `storage`,
`logging`, the agent's IPC port and the `api` section need a restart; they
are listed in the `Service` sensor's `restart_needed` attribute and in the
reload result.
