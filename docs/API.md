# Local REST / WebSocket API and setup page

An optional HTTP server inside the service, next to MQTT. Off by default.

```yaml
api:
  enabled: true
  host: 127.0.0.1          # loopback only unless a token is set
  port: 47392
  token: change-me         # required for every write; required for everything off loopback
  allow_config_edit: true  # the setup page may save config.yaml
```

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
| GET | `/api/events?n=50&user=kevin` | recent events, newest first |
| POST | `/api/command` | run a JSON command (below); returns `{ok, error, ...}` |
| POST | `/api/reload` | hot-reload `config.yaml` |
| GET | `/api/config` | `config.yaml` as text |
| PUT | `/api/config` | validate, write and reload the YAML in the body |
| GET | `/api/ws` | WebSocket (below) |

### Commands

The body of `POST /api/command` is the same JSON the MQTT `cmd` topic
accepts (see [HOMEASSISTANT.md](HOMEASSISTANT.md#free-form-command-topic)),
for example:

```json
{"action": "add_minutes", "user": "kevin", "minutes": 20, "pin": "1234"}
{"action": "focus", "user": "kevin", "minutes": 45, "allow": ["docs.google.com"]}
{"action": "rule_enabled", "user": "kevin", "rule": "YouTube", "enabled": false}
{"action": "screenshot", "user": "kevin"}
{"action": "launch", "name": "Minecraft", "user": "kevin"}
```

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
