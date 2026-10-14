"""Write the integration's strings.json and translations/en.json.

Usage: python scripts/gen_ha_strings.py [repository root, default: this script's parent folder]

The action texts live here.  The device triggers do not: their types and labels are read
from pclink/entities.py (USER_TRIGGERS / PC_TRIGGERS), the place that defines the events,
and the copy of the type lists in custom_components/pclink/const.py is rewritten to match.
So after an event type is added on the PC side, running this script is all there is to do.
Nothing is written unless every check passes.
"""
import ast, json, pathlib, re, sys, yaml
ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
device = {"name": "PC", "description": "The PC to send this to. Optional when only one PC is configured. PC Link (all PCs) sends it to every PC, for the actions where that makes sense."}
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
    "set_limit": svc("Set limit", "Override a user's limit for today, for weekdays or weekend days, or for the week. Without minutes the override is dropped and the limit from the PC's configuration applies again; the text unlimited (in YAML) removes the limit.", device_id=device, user=user,
                     scope={"name": "Scope", "description": "Which limit to change."},
                     minutes={"name": "Minutes", "description": "New limit in minutes. Leave empty to return to the configured limit; unlimited (in YAML) for no limit."}, pin=pin),
    "clear_overrides": svc("Clear overrides", "Return a user to the limits in the configuration file.", device_id=device, user=user, pin=pin),
    "block": svc("Pause access", "Pause a user's computer access now (log off and block login).", device_id=device, user=user),
    "unblock": svc("Resume access", "Lift a manual pause.", device_id=device, user=user, pin=pin),
    "focus": svc("Start focus mode", "Only allow-listed apps and sites for a period.", device_id=device, user=user,
                 minutes={"name": "Minutes", "description": "Duration; defaults to the user's configured focus duration."},
                 allow={"name": "Extra allowed", "description": "Additional apps or sites to allow this time (list)."}, pin=pin),
    "focus_off": svc("End focus mode", "End focus mode early.", device_id=device, user=user, pin=pin),
    "extend_bedtime": svc("Extend bedtime", "Push tonight's bedtime later.", device_id=device, user=user,
                          minutes={"name": "Minutes", "description": "How much later."}, pin=pin),
    "rule_enabled": svc("Enable or disable a rule", "Turn a per-app or per-site rule on or off for a user.", device_id=device, user=user,
                        rule={"name": "Rule", "description": "Rule name as configured on the PC."},
                        enabled={"name": "Enabled", "description": "Whether the rule is active."}, pin=pin),
    "rule_limit": svc("Set rule limit", "Change today's minutes, or the weekly budget, of a per-app or per-site rule.", device_id=device, user=user,
                      rule={"name": "Rule", "description": "Rule name as configured on the PC."},
                      minutes={"name": "Minutes", "description": "Minutes allowed today, or the weekly budget. Leave empty to return to the configured one; unlimited (in YAML) for no limit (for the weekly budget 0 also removes it)."},
                      scope={"name": "Scope", "description": "Today's limit (the default) or the rule's weekly budget."}, pin=pin),
    "set_rule": svc("Create or change a rule", "Create a per-app or per-site rule for a user, or change one - also one from the PC's configuration file. Only the fields given change. Loosening a rule needs the parent PIN.",
                    device_id=device, user=user,
                    rule={"name": "Rule", "description": "Name of the rule. An unknown name creates a new rule."},
                    apps={"name": "Programs", "description": "Process names the rule covers, such as RobloxPlayerBeta.exe."},
                    sites={"name": "Websites", "description": "Websites the rule covers, such as roblox.com."},
                    categories={"name": "Categories", "description": "Site categories (activity.site_categories on the PC) or gaming."},
                    daily_limit_minutes={"name": "Daily limit", "description": "Minutes per day: a number, or per day such as weekdays 30 and weekend 60. The text unlimited removes it."},
                    weekly_limit_minutes={"name": "Weekly limit", "description": "Minutes per week. The text unlimited removes it."},
                    allowed_hours={"name": "Allowed hours", "description": "When it is allowed, such as 16:00-19:00, or per day. The text all removes the restriction."},
                    blocked={"name": "Blocked", "description": "Never allowed."},
                    rule_action={"name": "When not allowed", "description": "Close the program or tab, or only warn."},
                    message={"name": "Message", "description": "Shown to the user instead of the standard text."}, pin=pin),
    "remove_rule": svc("Remove a rule", "Remove a rule made with Create or change a rule, or the changes made that way to a rule from the PC's configuration file.",
                       device_id=device, user=user,
                       rule={"name": "Rule", "description": "Name of the rule."}, pin=pin),
    "display": svc("Display", "Turn the display of a user's seat off or on (leave the user empty for every seat).", device_id=device, user=opt_user,
                   enabled={"name": "On", "description": "On wakes the display, off turns it off."}),
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
    "launch": svc("Launch app", "Start a configured app, or any command line, in a user's session. Only Home Assistant administrators may call it.", device_id=device,
                  name={"name": "App", "description": "Name of an app from the apps: section."},
                  command={"name": "Command", "description": "Command line to run instead of a configured app. Needs the PC's admin token when api.admin_token is set on the PC; a configured app does not."}, user=opt_user),
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
                          present={"name": "Present", "description": "True when a parent is home."}, pin=pin),
    "answer_time_request": svc("Answer a time request", "Approve or decline the extra time a user asked for on the PC.", device_id=device, user=user,
                               approve={"name": "Approve", "description": "On grants the request, off declines it."},
                               minutes={"name": "Minutes", "description": "How many minutes to grant; leave empty for what was asked for."}, pin=pin),
    "block_item": svc("Block app or website", "Close a program or a website whenever a user opens it.", device_id=device, user=user,
                      item={"name": "App or website", "description": "A program name such as roblox.exe, or a website such as youtube.com."}),
    "unblock_item": svc("Unblock app or website", "Remove a program or website from a user's block list.", device_id=device, user=user,
                        item={"name": "App or website", "description": "The entry to remove, or all to clear the list."}, pin=pin),
    "usage_report": svc("Usage report", "Applications, games and websites a user used in the last hours, longest first. Returns the report as response data.",
                        device_id=device, user=user,
                        hours={"name": "Hours", "description": "How far back to look (default: the PC's activity.report_hours, 10)."}),
    "set_profile": svc("Set profile", "Switch a user (or, without a user, everyone who has it) to a profile from the PC's configuration, such as holiday. A profile that gives more time needs the parent PIN.",
                       device_id=device, user={"name": "User", "description": "Windows login name; leave empty for every user that has the profile."},
                       profile={"name": "Profile", "description": "Name of the profile; normal returns to the usual limits."}, pin=pin),
    "reload_config": svc("Reload configuration", "Reload config.yaml on the PC.", device_id=device),
    "command": svc("Raw command", "Send any JSON command the PC's API accepts.", device_id=device,
                   data={"name": "Command", "description": "The command as a JSON object with an action key plus its fields, for example action add_minutes with user kevin and minutes 20."}, pin=pin),  # no braces: Home Assistant reads them as placeholders
}
def _assigned(path, name):
    """The literal assigned to ``name`` at module level of ``path`` (no import needed)."""
    for node in ast.parse((ROOT / path).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
            return ast.literal_eval(node.value)
    raise SystemExit(f"error: {name} not found in {path}")


def _trigger_labels(name):
    """type -> label from pclink/entities.py, checked: the labels end up in the automation editor."""
    labels = _assigned("pclink/entities.py", name)
    if not isinstance(labels, dict) or not labels:
        raise SystemExit(f"error: {name} in pclink/entities.py must be a non-empty dict of type -> label")
    for kind, label in labels.items():
        if not (isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9_]*", kind)):
            raise SystemExit(f"error: {name}: {kind!r} is not a usable trigger type (lower case, digits, underscores)")
        if not (isinstance(label, str) and label.strip()) or "{" in label or "}" in label:
            raise SystemExit(f"error: {name}: the label of {kind} must be a text without braces")
    return {kind: label.strip() for kind, label in labels.items()}


# "ran out of time" -> "{subtype} ran out of time" (the subtype is the user); "service started" -> "Service started"
user_triggers = {kind: "{subtype} " + label for kind, label in _trigger_labels("USER_TRIGGERS").items()}
pc_triggers = {kind: label[0].upper() + label[1:] for kind, label in _trigger_labels("PC_TRIGGERS").items()}
both = set(user_triggers) & set(pc_triggers)
if both:
    raise SystemExit(f"error: trigger types used for users and for the PC (one label each is possible): {sorted(both)}")

address = {"host": "Host", "port": "Port", "token": "API token", "tls": "HTTPS", "fingerprint": "Certificate fingerprint"}
address_help = {
    "host": "IP address or host name of the PC.",
    "port": "api.port on the PC (default 47392).",
    "tls": "Switch on when the PC serves HTTPS (api.tls_cert and api.tls_key in its config.yaml).",
    "fingerprint": "SHA-256 fingerprint of the PC's certificate, as `pclink make-cert` prints it. Needed for a self-signed certificate; only that certificate is then accepted.",
}
errors = {
    "cannot_connect": "Could not connect. Check the address, that api.enabled is true, api.host is 0.0.0.0 and the Windows firewall allows the port.",
    "invalid_auth": "The token was rejected, or it is the read-only token.",
    "invalid_host": "That is not a host name or an address.",
    "invalid_fingerprint": "A SHA-256 fingerprint has 64 hexadecimal digits.",
    "tls_error": "The PC's certificate was not accepted. Check the fingerprint, and that HTTPS is switched on here exactly when the PC serves it.",
    "outdated": "The PC Link service on that PC is too old for this integration; update it.",
    "wrong_device": "That address belongs to a different PC.",
    "token_required": "Enter the API token: it is only kept for the address the PC has now, or when the PC has to show the pinned certificate.",
    "too_many_attempts": "The PC refuses this address for a while after too many wrong tokens. Wait a few minutes and try again.",
    "busy": "The PC has no connection free right now. Try again in a few seconds.",
    "unknown": "Unexpected error.",
}
# the token field of the address forms of an existing PC
keep_token = "Leave empty to keep the current token - only possible for the current address, or with the certificate fingerprint left as it is."
strings = {
    "config": {
        "flow_title": "{name}",
        "step": {
            "user": {
                "title": "Add a PC",
                "description": "On the PC, enable the local API in config.yaml (run `python -m pclink api-token` for a ready-made block) and restart the service.",
                "data": dict(address),
                "data_description": {**address_help, "token": "api.token from the PC's config.yaml."},
            },
            "zeroconf_confirm": {
                "title": "Add {name}",
                "description": "{name} was found at {host}. Connection: {security}. Anything on the network can announce itself, so compare this with what setup-api printed on the PC before you enter its API token (api.token in config.yaml on the PC).",
                "data": {"token": "API token"},
            },
            "reauth_confirm": {
                "title": "New token for {name}",
                "description": "The PC rejected the stored token. Enter the current api.token from its config.yaml.",
                "data": {"token": "API token"},
            },
            "reconfigure": {
                "title": "Reconfigure {name}",
                "description": "Change the address, port or token of this PC. Its connection now: {security}.",
                "data": dict(address),
                "data_description": {**address_help, "token": keep_token},
            },
        },
        "error": dict(errors),
        "abort": {
            "already_configured": "This PC is already configured.",
            "reauth_successful": "The token was updated.",
            "reconfigure_successful": "The PC was reconfigured.",
            "no_id": "The discovered service did not identify itself.",
            "wrong_device": "The PC at that address is not the one that was discovered.",
            "unknown": "Unexpected error.",
        },
    },
    "device_automation": {"trigger_type": {**user_triggers, **pc_triggers}},
    "issues": {
        "wrong_pc": {
            "title": "{found_name} answers at {address}, the address configured for {name}",
            "fix_flow": {
                "step": {"address": {
                    "title": "Where is {name} now?",
                    "description": "The address configured for this PC is now used by another PC Link PC, so nothing is read from it or sent to it. Enter the address {name} has now, and its API token unless the certificate stays pinned. Its connection now: {security}.",
                    "data": dict(address),
                    "data_description": {**address_help, "token": keep_token},
                }},
                "error": dict(errors),
                "abort": {"unknown": "The PC is no longer configured."},
            },
        },
        "pc_moved": {
            "title": "{name} announced itself at a new address",
            "fix_flow": {
                "step": {"address": {
                    "title": "Use the new address of {name}?",
                    "description": "The PC cannot be reached at the configured address but announced itself on the network at another one, which is filled in below. Anything on the network can send such an announcement: check the address, and enter the API token again - only a PC that has to show the pinned certificate gets the stored one. The PC there has to be {name}. Its connection now: {security}.",
                    "data": dict(address),
                    "data_description": {**address_help, "token": keep_token},
                }},
                "error": dict(errors),
                "abort": {"unknown": "The PC is no longer configured."},
            },
        },
    },
    "services": services,
    # errors raised by actions and entity commands (HomeAssistantError with a translation_key)
    "exceptions": {
        "throttled": {"message": "{pc} asked to wait {seconds} s before it takes commands again; nothing was changed."},
    },
}
# -- consistency checks: all of them run BEFORE anything is written -------------------------
problems = []
svc_yaml = yaml.safe_load((ROOT / "custom_components/pclink/services.yaml").read_text(encoding="utf-8"))
if set(svc_yaml) != set(services):
    problems.append(f"actions only in services.yaml or only in this script: {sorted(set(svc_yaml) ^ set(services))}")
for name, spec in svc_yaml.items():
    if name in services and set((spec or {}).get("fields", {})) != set(services[name]["fields"]):
        problems.append(f"{name}: fields only in services.yaml or only in this script: "
                        f"{sorted(set((spec or {}).get('fields', {})) ^ set(services[name]['fields']))}")
if problems:
    raise SystemExit("error: " + "\n       ".join(problems))


def _list_source(name, kinds):
    """``NAME = [...]`` for const.py, wrapped like the hand-written original."""
    lines, line = [], "   "
    for kind in kinds:
        item = f' "{kind}",'
        if len(line) + len(item) > 116:
            lines.append(line)
            line = "   "
        line += item
    return f"{name} = [\n" + "\n".join(lines + [line]) + "\n]"


# The integration's copy of the trigger types (const.py) follows pclink/entities.py.
const_path = ROOT / "custom_components/pclink/const.py"
const_text = new_const = const_path.read_text(encoding="utf-8")
for const_name, labels in (("USER_TRIGGERS", user_triggers), ("PC_TRIGGERS", pc_triggers)):
    if list(_assigned("custom_components/pclink/const.py", const_name)) == list(labels):
        continue  # already the same types in the same order: the file is left as it is
    new_const, count = re.subn(rf"^{const_name} = \[.*?^\]", lambda m, n=const_name, k=list(labels): _list_source(n, k),
                               new_const, count=1, flags=re.S | re.M)
    if count != 1:
        raise SystemExit(f"error: no '{const_name} = [...]' list found in {const_path}")
text = json.dumps(strings, indent=2, ensure_ascii=False) + "\n"
if new_const != const_text:
    const_path.write_text(new_const, encoding="utf-8")
    print("const.py: trigger types updated from pclink/entities.py")
(ROOT / "custom_components/pclink/strings.json").write_text(text, encoding="utf-8")
(ROOT / "custom_components/pclink/translations/en.json").write_text(text, encoding="utf-8")
print(f"services.yaml fields match; {len(user_triggers)} user and {len(pc_triggers)} PC trigger types; strings + translations written")
