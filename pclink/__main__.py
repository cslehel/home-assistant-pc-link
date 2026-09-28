"""Command line entry point: ``python -m pclink <command>``."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time

from . import __version__
from .config import Config, ConfigError, default_config_path
from .logsetup import setup_logging

SERVICE_COMMANDS = {"install", "remove", "update", "start", "stop", "restart", "debug", "harden", "unharden"}

USAGE = f"""home-assistant-pc-link {__version__}

usage: python -m pclink <command> [options]

commands:
  run [--config PATH]        run the service in the foreground (console)
  install [--no-harden]      install the Windows service (needs an admin prompt);
                             tamper resistance is applied unless --no-harden
  remove | start | stop | restart
                             manage the Windows service (needs an admin prompt)
  harden | unharden          enable / disable tamper resistance on the service
  check-config [--config]    validate the configuration file
  init-config [--path]       write an example configuration file
  pin-hash <pin>             print a parent_pin_sha256 value for the config
  unblock-all [--config]     restore logon hours / enable all managed accounts
  test-mqtt [--config]       connect to the broker and publish a test message
  agent                      (internal) per-session user agent
  version
"""


def _config_arg(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=None)
    args, _ = parser.parse_known_args(argv)
    return args.config or default_config_path()


def cmd_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pclink run")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)
    path = args.config or default_config_path()
    try:
        cfg = Config.load(path)
    except FileNotFoundError:
        print(f"config not found: {path}\nrun 'python -m pclink init-config' first", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.logging.path, args.log_level or cfg.logging.level, console=True,
                  max_bytes=cfg.logging.max_bytes, backups=cfg.logging.backups)
    from .core import build_orchestrator

    orch = build_orchestrator(cfg)
    if sys.platform == "win32":
        _install_session_change_listener(orch)
    print(f"pclink {__version__} running (config: {path}); press Ctrl+C to stop")
    try:
        orch.run()
    except KeyboardInterrupt:
        orch.stop()
    return 0


def _install_session_change_listener(orch) -> None:
    """Console mode has no SCM callbacks; poll a little faster instead."""
    orch.cfg.time_limits.tick_seconds = min(orch.cfg.time_limits.tick_seconds, 3.0)


def cmd_check_config(argv: list[str]) -> int:
    path = _config_arg(argv)
    try:
        cfg = Config.load(path)
    except FileNotFoundError:
        print(f"config not found: {path}")
        return 2
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    days_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    tl = cfg.time_limits
    print(f"config OK: {path}")
    print(f"  device: {cfg.device.name} (id {cfg.device.id})")
    print(f"  mqtt:   {cfg.mqtt.host}:{cfg.mqtt.port} (discovery prefix {cfg.mqtt.discovery_prefix})")
    print(f"  block:  {tl.block_method}; parent PIN: {'set' if tl.pin_enabled else 'not set'}; "
          f"shared budget: {tl.shared_budget}; week starts {tl.week_start}")
    extras = []
    if cfg.api.enabled:
        extras.append(f"api http://{cfg.api.host}:{cfg.api.port}/" + ("" if cfg.api.token else " (read-only: no token)"))
    if cfg.screenshots.enabled:
        extras.append("screenshots" + (" (PIN)" if cfg.screenshots.require_pin else ""))
    if cfg.windows_update.enabled:
        extras.append("windows update")
    if cfg.fleet.enabled:
        extras.append("fleet roll-up")
    if cfg.apps:
        extras.append(f"{len(cfg.apps)} app(s)")
    if extras:
        print("  extras: " + ", ".join(extras))
    for p in tl.users:
        if p.unlimited:
            print(f"  user {p.name}: unlimited (statistics only)")
            continue
        days = ", ".join(f"{d}={'-' if p.daily_limits[i] is None else p.daily_limits[i]}" for i, d in enumerate(days_names))
        print(f"  user {p.name}: minutes/day {days}; action={p.action}; warn={p.warn_at_minutes}")
        if p.weekly_limit_minutes is not None or p.rollover_max_minutes:
            print(f"      weekly limit {p.weekly_limit_minutes or '-'} min; rollover up to {p.rollover_max_minutes} min")
        for i, day in enumerate(days_names):
            w = p.allowed_windows.get(i) or []
            if w:
                print(f"      {day}: allowed " + ", ".join(f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in w))
        if p.has_bedtime:
            beds = ", ".join(f"{d}={'-' if p.bedtime[i] is None else f'{p.bedtime[i] // 60:02d}:{p.bedtime[i] % 60:02d}'}" for i, d in enumerate(days_names))
            print(f"      bedtime {beds}; wake {p.wake_time[0] // 60:02d}:{p.wake_time[0] % 60:02d}"
                  + (f"; +{p.bedtime_parent_extension_minutes} min while a parent is home" if p.bedtime_parent_extension_minutes else ""))
        for r in p.rules:
            what = ", ".join(r.apps + r.sites + [f"category:{c}" for c in r.categories])
            if r.blocked:
                print(f"      rule {r.name}: blocked ({what})")
            else:
                lim = r.daily_limits[0] if r.daily_limits[0] == r.daily_limits[5] else f"{r.daily_limits[0]}/{r.daily_limits[5]}"
                print(f"      rule {r.name}: {lim} min/day, action={r.action} ({what})")
        if p.focus.allow_apps or p.focus.allow_sites or p.focus.scheduled:
            print(f"      focus: apps {p.focus.allow_apps} sites {p.focus.allow_sites} default {p.focus.default_minutes} min"
                  + (" (scheduled)" if p.focus.scheduled else ""))
        if p.breaks.enabled:
            print(f"      breaks: every {p.breaks.every_minutes} min")
        if p.rewards:
            print("      rewards: " + ", ".join(f"{r.name} (+{r.minutes})" for r in p.rewards))
        print(f"      presence: count_idle={p.count_idle} (after {p.idle_threshold_seconds // 60} min), "
              f"count_away={p.counts_away()}, count_passive={p.count_passive}")
    return 0


def cmd_pin_hash(argv: list[str]) -> int:
    from .pin import hash_pin

    if not argv:
        print("usage: python -m pclink pin-hash <pin>", file=sys.stderr)
        return 2
    print(f"parent_pin_sha256: {hash_pin(argv[0])}")
    return 0


def cmd_init_config(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pclink init-config")
    parser.add_argument("--path", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    target = args.path or default_config_path()
    if os.path.exists(target) and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(here, "config.example.yaml"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.yaml")]
    src = next((c for c in candidates if os.path.isfile(c)), None)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if src:
        shutil.copyfile(src, target)
    else:
        from .example_config import EXAMPLE_CONFIG

        with open(target, "w", encoding="utf-8") as fh:
            fh.write(EXAMPLE_CONFIG)
    print(f"wrote {target}; edit the MQTT settings and the users, then run 'python -m pclink check-config'")
    return 0


def cmd_unblock_all(argv: list[str]) -> int:
    path = _config_arg(argv)
    cfg = Config.load(path)
    from .logonblock import make_blocker

    blocker = make_blocker(cfg.time_limits.block_method)
    for p in cfg.time_limits.users:
        if p.unlimited:
            continue
        ok = blocker.unblock(p.name)
        print(f"{p.name}: {'unblocked' if ok else 'FAILED'}")
    return 0


def cmd_test_mqtt(argv: list[str]) -> int:
    path = _config_arg(argv)
    cfg = Config.load(path)
    setup_logging(cfg.logging.path, "INFO", console=True)
    from .ha import MqttBridge

    done = threading.Event()

    def connected():
        print("connected; publishing test message")
        bridge.publish(f"{bridge.base}/test", {"hello": "home assistant", "version": __version__})
        done.set()

    bridge = MqttBridge(cfg.mqtt, cfg.device.id, on_command=lambda *_: None, on_connected=connected)
    bridge.start()
    ok = done.wait(15)
    time.sleep(0.5)
    bridge.stop()
    print("OK" if ok else "FAILED: could not connect within 15 s (check host/port/credentials)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "version":
        print(__version__)
        return 0
    if cmd == "run":
        return cmd_run(rest)
    if cmd == "agent":
        from .agent.main import main as agent_main

        return agent_main(rest)
    if cmd in SERVICE_COMMANDS or any(a in SERVICE_COMMANDS for a in argv):
        from .service import handle_command_line

        handle_command_line(argv)
        return 0
    if cmd == "check-config":
        return cmd_check_config(rest)
    if cmd == "init-config":
        return cmd_init_config(rest)
    if cmd == "pin-hash":
        return cmd_pin_hash(rest)
    if cmd == "unblock-all":
        return cmd_unblock_all(rest)
    if cmd == "test-mqtt":
        return cmd_test_mqtt(rest)
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
