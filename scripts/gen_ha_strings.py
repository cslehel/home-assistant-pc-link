import json, pathlib, sys, yaml
ROOT = pathlib.Path(sys.argv[1])
device = {"name": "PC", "description": "The PC to send this to. Optional when only one PC is configured."}
user = {"name": "User", "description": "Windows login name of the user."}
opt_user = {"name": "User", "description": "Windows login name; leave empty for every active session."}
pin = {"name": "Parent PIN", "description": "Needed for actions that loosen a limit when a parent PIN is configured and not unlocked."}
def svc(title_, desc_, /, **fields):
    return {"name": title_, "description": desc_, "fields": fields}
services = {
    "add_minutes": svc("Add minutes", "Give a user extra screen time for today.", device_id=device, user=user,
                       minutes={"name": "Minutes", "description": "How many minutes to add."}, pin=pin),
    "earn": svc("Earn minutes", "Add bonus minutes recorded as earned (for example after a chore).", device_id=device, user=user,
                minutes={"name": "Minutes", "description": "How many minutes were earned."},
                reason={"name": "Reason", "description": "Shown in the event log and statistics."}, pin=pin),
    "reward": svc("Claim reward", "Claim one of the configured rewards for a user.", device_id=device, user=user,
                  name={"name": "Reward", "description": "Name of the reward as configured on the PC."}, pin=pin),
    "set_limit": svc("Set limit", "Override a user's daily or weekly limit. Omit minutes to remove the limit.", device_id=device, user=user,
                     scope={"name": "Scope", "description": "Which limit to change."},
                     minutes={"name": "Minutes", "description": "New limit in minutes; leave empty for unlimited."}, pin=pin),
    "clear_overrides": svc("Clear overrides", "Return a user to the limits in the configuration file.", device_id=device, user=user, pin=pin),
    "block": svc("Pause access", "Pause a user's computer access now (log off and block login).", device_id=device, user=user),
    "unblock": svc("Resume access", "Lift a manual pause.", device_id=device, user=user, pin=pin),
    "focus": svc("Start focus mode", "Only allow-listed apps and sites for a period.", device_id=device, user=user,
                 minutes={"name": "Minutes", "description": "Duration; defaults to the user's configured focus duration."},
                 allow={"name": "Extra allowed", "description": "Additional apps or sites to allow this time (list)."}),
    "focus_off": svc("End focus mode", "End focus mode early.", device_id=device, user=user, pin=pin),
    "extend_bedtime": svc("Extend bedtime", "Push tonight's bedtime later.", device_id=device, user=user,
                          minutes={"name": "Minutes", "description": "How much later."}, pin=pin),
    "rule_enabled": svc("Enable or disable a rule", "Turn a per-app or per-site rule on or off for a user.", device_id=device, user=user,
                        rule={"name": "Rule", "description": "Rule name as configured on the PC."},
                        enabled={"name": "Enabled", "description": "Whether the rule is active."}, pin=pin),
    "rule_limit": svc("Set rule limit", "Change today's minutes for a per-app or per-site rule.", device_id=device, user=user,
                      rule={"name": "Rule", "description": "Rule name as configured on the PC."},
                      minutes={"name": "Minutes", "description": "Minutes allowed today; leave empty for unlimited."}, pin=pin),
    "breaks": svc("Break reminders", "Turn a user's break reminders on or off.", device_id=device, user=user,
                  enabled={"name": "Enabled", "description": "Whether reminders are shown."}),
    "notify": svc("Send message", "Show a message on the PC (to one user or to everyone).", device_id=device, user=opt_user,
                  title={"name": "Title", "description": "Message title."}, message={"name": "Message", "description": "Text to show."},
                  level={"name": "Level", "description": "Info, warning or error."}),
    "logoff": svc("Log off", "Log a user (or everyone) off.", device_id=device, user=opt_user),
    "lock": svc("Lock", "Lock a user's session (or all sessions).", device_id=device, user=opt_user),
    "volume": svc("Volume", "Set the volume or mute in a user's session.", device_id=device, user=opt_user,
                  level={"name": "Level", "description": "Volume 0-100."}, muted={"name": "Muted", "description": "Mute or unmute."}),
    "screenshot": svc("Take screenshot", "Take a screenshot of a user's screen (the user is told).", device_id=device, user=user, pin=pin),
    "launch": svc("Launch app", "Start a configured app, or any command line, in a user's session.", device_id=device,
                  name={"name": "App", "description": "Name of an app from the apps: section."},
                  command={"name": "Command", "description": "Command line to run instead of a configured app."}, user=opt_user),
    "close_app": svc("Close app", "Close a configured app or a process in a user's session.", device_id=device,
                     name={"name": "App", "description": "Name of an app from the apps: section."},
                     process={"name": "Process", "description": "Process name to close instead, e.g. javaw.exe."}, user=opt_user),
    "power": svc("Power", "Shut down, reboot, sleep or hibernate the PC, or cancel a pending shutdown.", device_id=device,
                 action={"name": "Action", "description": "What to do."},
                 delay={"name": "Delay", "description": "Seconds before shutting down or rebooting."},
                 message={"name": "Message", "description": "Shown to logged-in users."}),
    "wake_on_lan": svc("Wake another PC", "Send a Wake-on-LAN packet from this PC.", device_id=device,
                       mac={"name": "MAC address", "description": "MAC of the PC to wake."},
                       broadcast={"name": "Broadcast address", "description": "Defaults to the PC's configured broadcast."},
                       port={"name": "Port", "description": "UDP port, usually 9."}),
    "pin": svc("Unlock parent PIN", "Unlock the parent PIN for the configured timeout.", device_id=device,
               pin={"name": "Parent PIN", "description": "The PIN."}),
    "pin_lock": svc("Lock parent PIN", "Re-lock the parent PIN immediately.", device_id=device),
    "parent_present": svc("Parent at home", "Tell the PC whether a parent is home (auto-extends bedtimes).", device_id=device,
                          present={"name": "Present", "description": "True when a parent is home."}),
    "reload_config": svc("Reload configuration", "Reload config.yaml on the PC.", device_id=device),
    "command": svc("Raw command", "Send any JSON command the PC's API accepts.", device_id=device,
                   data={"name": "Command", "description": "The JSON object, e.g. {\"action\": \"add_minutes\", \"user\": \"kevin\", \"minutes\": 20}."}, pin=pin),
}
user_triggers = {
    "time_up": "{subtype} ran out of time", "warning": "{subtype} is about to run out of time",
    "grace_started": "{subtype} entered the grace period", "logged_in": "{subtype} logged in", "logged_off": "{subtype} logged off",
    "login_blocked": "{subtype} got login blocked", "login_unblocked": "{subtype} got login unblocked",
    "bedtime": "{subtype} reached bedtime", "focus_started": "{subtype} started focus mode", "focus_ended": "{subtype} ended focus mode",
    "break_due": "{subtype} is due for a break", "app_limit_reached": "{subtype} hit an app or site limit",
    "app_closed": "{subtype} had an app or tab closed", "reward_claimed": "{subtype} earned bonus minutes",
    "screenshot_taken": "{subtype} had a screenshot taken", "presence_away": "{subtype} left the seat",
    "presence_back": "{subtype} came back to the seat",
}
pc_triggers = {
    "started": "Service started", "config_reloaded": "Configuration reloaded", "parent_unlocked": "Parent PIN entered",
    "pin_wrong": "Wrong parent PIN entered", "pin_locked_out": "Parent PIN locked out", "reboot_pending": "Needs a reboot for updates",
    "updates_available": "Has Windows updates pending", "usb_changed": "USB devices changed", "printer_error": "Printer reported an error",
}
strings = {
    "config": {
        "flow_title": "{name}",
        "step": {
            "user": {
                "title": "Add a PC",
                "description": "On the PC, enable the local API in config.yaml (run `python -m pclink api-token` for a ready-made block) and restart the service.",
                "data": {"host": "Host", "port": "Port", "token": "API token"},
                "data_description": {"host": "IP address or host name of the PC.", "port": "api.port on the PC (default 47392).", "token": "api.token from the PC's config.yaml."},
            },
            "zeroconf_confirm": {
                "title": "Add {name}",
                "description": "{name} was found at {host}. Enter its API token (api.token in config.yaml on the PC).",
                "data": {"token": "API token"},
            },
            "reauth_confirm": {
                "title": "New token for {name}",
                "description": "The PC rejected the stored token. Enter the current api.token from its config.yaml.",
                "data": {"token": "API token"},
            },
            "reconfigure": {
                "title": "Reconfigure {name}",
                "description": "Change the address, port or token of this PC.",
                "data": {"host": "Host", "port": "Port", "token": "API token"},
            },
        },
        "error": {
            "cannot_connect": "Could not connect. Check the address, that api.enabled is true, api.host is 0.0.0.0 and the Windows firewall allows the port.",
            "invalid_auth": "The token was rejected.",
            "outdated": "The PC Link service on that PC is too old for this integration; update it.",
            "wrong_device": "That address belongs to a different PC.",
            "unknown": "Unexpected error.",
        },
        "abort": {
            "already_configured": "This PC is already configured.",
            "reauth_successful": "The token was updated.",
            "reconfigure_successful": "The PC was reconfigured.",
            "no_id": "The discovered service did not identify itself.",
            "unknown": "Unexpected error.",
        },
    },
    "device_automation": {"trigger_type": {**user_triggers, **pc_triggers}},
    "services": services,
}
text = json.dumps(strings, indent=2, ensure_ascii=False) + "\n"
(ROOT / "custom_components/pclink/strings.json").write_text(text)
(ROOT / "custom_components/pclink/translations/en.json").write_text(text)
svc_yaml = yaml.safe_load(open(ROOT / "custom_components/pclink/services.yaml"))
assert set(svc_yaml) == set(services), set(svc_yaml) ^ set(services)
for name, spec in svc_yaml.items():
    assert set(spec.get("fields", {})) == set(services[name]["fields"]), (name, set(spec.get("fields", {})) ^ set(services[name]["fields"]))
print("strings + translations written; services.yaml fields match")
