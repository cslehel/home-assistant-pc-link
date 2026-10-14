"""Command line entry point: ``pclink.exe <command>`` or ``python -m pclink <command>``."""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Optional

from . import __version__
from .config import Config, ConfigError, data_dir, default_config_path
from .logsetup import setup_logging
from .runtime import AGENT_EXE, SERVICE_HOST_ARG, app_dir, cli_name, is_frozen

SERVICE_COMMANDS = {"install", "remove", "update", "start", "stop", "restart", "debug", "harden", "unharden"}

USAGE = """home-assistant-pc-link {version}

usage: {cli} <command> [options]

commands:
  run [--config PATH]        run the service in the foreground (console)
  install [--no-harden]      install the Windows service (needs an admin prompt);
                             tamper resistance is applied unless --no-harden
  remove | start | stop | restart
                             manage the Windows service (needs an admin prompt)
  harden | unharden          enable / disable tamper resistance on the service
  check-config [--config]    validate the configuration file
  init-config [--path]       write an example configuration file
  pin-hash [<pin>]           print a parent_pin_hash value for the config (asks for the
                             PIN when none is given, which keeps it out of the history)
  unblock-all [--config]     restore logon hours / enable all managed accounts
  setup-api [--config]       set up the local API for the Home Assistant integration in
                             config.yaml: on for the LAN, a token, HTTPS with a certificate
                             of its own; restarts the service and prints the token
                             (needs an admin prompt; keeps an existing token and certificate)
  api-token                  print a fresh API token and an api: block to paste in by hand
  make-cert [--force]        create a certificate for HTTPS on the local API and print
                             the api: lines and its fingerprint (needs an admin prompt)
  api-info [--config]        print how the local API is configured, as JSON (used by
                             the installers for the firewall rule)
  selftest [--require a,b]   show which components are available (optional backends,
                             bundled modules); exit code 1 if a required one is missing
  agent                      (internal) per-session user agent
  service                    (internal) service host started by Windows
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
        print(f"config not found: {path}\nrun '{cli_name()} init-config' first", file=sys.stderr)
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
    except OSError as exc:  # the data folder is for administrators only
        print(f"config cannot be read: {exc} (run this from an administrator prompt)")
        return 2
    days_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    tl = cfg.time_limits
    print(f"config OK: {path}")
    print(f"  device: {cfg.device.name} (id {cfg.device.id})")
    if cfg.api.enabled:
        reach = "LAN" if cfg.api.host not in ("127.0.0.1", "localhost", "::1") else "this PC only"
        reads = "reads need a token" if cfg.api.read_token else "reads on this PC need no token"
        tls = bool(getattr(cfg.api, "tls_cert", None))
        print(f"  api:    {'https' if tls else 'http'}://{cfg.api.host}:{cfg.api.port}/ ({reach}; "
              f"token {'set' if cfg.api.token else 'NOT set - read-only'}; "
              f"admin token {'set' if getattr(cfg.api, 'admin_token', None) else 'not set'}; "
              f"{reads}; mDNS {'on' if cfg.api.advertise else 'off'})")
        if reach == "LAN" and not tls:
            print(f"          note: without HTTPS the token crosses the network in the clear ('{cli_name()} make-cert')")
    else:
        print(f"  WARNING: the API is disabled; Home Assistant cannot reach this PC ('{cli_name()} api-token' "
              "prints the api: block to paste in; see docs/HOMEASSISTANT.md)")
    print(f"  block:  {tl.block_method}; parent PIN: {'set' if tl.pin_enabled else 'not set'}; "
          f"shared budget: {tl.shared_budget}; week starts {tl.week_start}; time zone: {tl.time_zone or 'the PC setting'}")
    extras = []
    if cfg.api.enabled:
        extras.append(f"api http://{cfg.api.host}:{cfg.api.port}/" + ("" if cfg.api.token else " (read-only: no token)"))
    if cfg.screenshots.enabled:
        extras.append("screenshots" + (" (PIN)" if cfg.screenshots.require_pin else ""))
    if cfg.windows_update.enabled:
        extras.append("windows update")
    if cfg.fleet.enabled and tl.shared_budget:
        extras.append("shared budget across PCs")
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
                week = f", {r.weekly_limit_minutes} min/week" if r.has_weekly_limit else ""
                print(f"      rule {r.name}: {lim} min/day{week}, action={r.action} ({what})")
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

    if argv:
        pin = argv[0]
    else:   # asked for, so that the PIN is neither on the screen nor in the shell's history
        import getpass

        try:
            pin = getpass.getpass("Parent PIN: ")
            again = getpass.getpass("Once more: ")
        except (EOFError, KeyboardInterrupt):
            print(f"usage: {cli_name()} pin-hash [<pin>]", file=sys.stderr)
            return 2
        if pin != again:
            print("the two entries differ", file=sys.stderr)
            return 2
    if not str(pin).strip():
        print(f"usage: {cli_name()} pin-hash [<pin>]", file=sys.stderr)
        return 2
    # the salted form (time_limits.parent_pin_hash); the older parent_pin_sha256 is still read
    print(f"parent_pin_hash: {hash_pin(pin)}")
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
    candidates = [os.path.join(here, "config.example.yaml"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.yaml"),
                  os.path.join(app_dir(), "config.example.yaml")]
    src = next((c for c in candidates if os.path.isfile(c)), None)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.normcase(os.path.abspath(os.path.dirname(target))) == os.path.normcase(os.path.abspath(data_dir())):
        # the config holds the API tokens and the parent PIN: administrators only.
        # No configuration is written into a folder that could not be restricted.
        from .hardening import DataDirError, ensure_data_dir

        try:
            ensure_data_dir(data_dir())
        except DataDirError as exc:
            print(f"not writing the configuration: {exc}", file=sys.stderr)
            return 3
    if src:
        with open(src, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        from .example_config import EXAMPLE_CONFIG

        text = EXAMPLE_CONFIG
    # A new configuration is tied to the time zone the PC is in now: which day it is then no longer
    # follows Windows' time zone setting, which a standard user is allowed to change.
    try:
        from .util import local_iana_zone

        zone = local_iana_zone()
    except Exception:  # noqa: BLE001 - never a reason not to write the configuration
        zone = None
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(with_time_zone(text, zone))
    print(f"wrote {target}; set up the api: section ('{cli_name()} api-token') and the users, "
          f"then run '{cli_name()} check-config'")
    if zone:
        print(f"time_limits.time_zone was set to {zone} (this PC's time zone); change it there if that is not right")
    else:
        print("this PC's time zone could not be worked out: set time_limits.time_zone in the file "
              "(for example Europe/Budapest)")
    return 0


_TIME_ZONE_HINT = re.compile(r"^([ \t]+)#[ \t]*time_zone:[^\n]*\n(?:[ \t]+#[ \t]{3,}[^\n]*\n)*", re.M)


def with_time_zone(text: str, zone: Optional[str]) -> str:
    """The example configuration with ``time_limits.time_zone`` set to ``zone`` (pure).

    The commented-out example line is replaced by the setting; when the zone is not
    known it is replaced by a note saying so.  A text that sets ``time_zone`` already,
    or has no ``time_limits`` section to put it in, is returned as it is."""
    if re.search(r"^[ \t]+time_zone:", text, re.M):
        return text
    if zone:
        def lines(indent: str) -> str:
            return (f"{indent}time_zone: {zone}\n"
                    f"{indent}# ^ this PC's time zone when the configuration was created. Days, allowed hours and\n"
                    f"{indent}#   bedtimes are reckoned in it, whatever Windows' time zone is set to later (a\n"
                    f"{indent}#   standard user may change that). Correct it here if the PC is somewhere else.\n")
    else:
        def lines(indent: str) -> str:
            return (f"{indent}# time_zone: Europe/Budapest\n"
                    f"{indent}# ^ SET THIS (remove the '#', put in your zone): this PC's time zone could not be\n"
                    f"{indent}#   worked out. Without it days, allowed hours and bedtimes follow Windows' time\n"
                    f"{indent}#   zone setting, which a standard user may change.\n")
    if _TIME_ZONE_HINT.search(text):
        return _TIME_ZONE_HINT.sub(lambda m: lines(m.group(1)), text, count=1)
    section = re.search(r"^time_limits:[^\n]*\n", text, re.M)
    if section is None:
        return text
    return text[:section.end()] + lines("  ") + text[section.end():]


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


def cmd_api_token(argv: list[str]) -> int:
    """Print a random token plus the ``api:`` block that enables the LAN API for the HA integration."""
    import secrets

    token = secrets.token_urlsafe(24)
    print(api_block(token))
    return 0


def api_block(token: str) -> str:
    return (
        "# paste into %ProgramData%\\PCLink\\config.yaml (then restart the service):\n"
        "api:\n"
        "  enabled: true\n"
        "  host: 0.0.0.0          # reachable from Home Assistant\n"
        "  port: 47392\n"
        f"  token: {token}\n"
        "\n"
        "# In Home Assistant: Settings -> Devices & services -> Add integration -> PC Link,\n"
        "# enter this PC's address, port 47392 and the token above.\n"
        f"# The token travels in the clear on your network unless the API uses HTTPS: '{cli_name()} make-cert'."
    )


# -- HTTPS for the local API ---------------------------------------------------------------------------

CERT_FILE, KEY_FILE = "api-cert.pem", "api-key.pem"

# Makes a key and a self-signed certificate in memory (nothing goes into a certificate store, no
# administrator rights needed) and prints them as JSON.  No double quotes: it travels as one argument.
_CERT_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider 2048; "
    "$rsa.PersistKeyInCsp = $false; "
    "$req = New-Object System.Security.Cryptography.X509Certificates.CertificateRequest("
    "('CN=' + $env:PCLINK_CERT_CN), $rsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256, "
    "[System.Security.Cryptography.RSASignaturePadding]::Pkcs1); "
    "$san = New-Object System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder; "
    "foreach ($n in ($env:PCLINK_CERT_NAMES -split '\\|')) { if ($n) { $ip = $null; "
    "if ([System.Net.IPAddress]::TryParse($n, [ref]$ip)) { $san.AddIpAddress($ip) } else { $san.AddDnsName($n) } } }; "
    "$req.CertificateExtensions.Add($san.Build()); "
    "$now = [System.DateTimeOffset]::UtcNow; "
    "$cert = $req.CreateSelfSigned($now.AddDays(-1), $now.AddDays([int]$env:PCLINK_CERT_DAYS)); "
    "$p = $rsa.ExportParameters($true); $b = { param($x) [System.Convert]::ToBase64String($x) }; "
    "[pscustomobject]@{ cert = (& $b $cert.RawData); n = (& $b $p.Modulus); e = (& $b $p.Exponent); d = (& $b $p.D); "
    "p = (& $b $p.P); q = (& $b $p.Q); dp = (& $b $p.DP); dq = (& $b $p.DQ); qi = (& $b $p.InverseQ) } "
    "| ConvertTo-Json -Compress"
)


def _pem(label: str, der: bytes) -> str:
    import base64

    text = base64.b64encode(der).decode("ascii")
    lines = "\n".join(text[i:i + 64] for i in range(0, len(text), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n"


def _der(tag: int, content: bytes) -> bytes:
    n = len(content)
    size = bytes([n]) if n < 128 else bytes([0x80 | len(n.to_bytes((n.bit_length() + 7) // 8, "big"))]) + \
        n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + size + content


def _der_int(value: bytes) -> bytes:
    value = value.lstrip(b"\x00") or b"\x00"
    return _der(0x02, (b"\x00" if value[0] & 0x80 else b"") + value)


def rsa_private_key_der(parts: list[bytes]) -> bytes:
    """PKCS#1 ``RSAPrivateKey`` from modulus, e, d, p, q, dp, dq, qinv (big-endian bytes)."""
    return _der(0x30, _der_int(b"\x00") + b"".join(_der_int(part) for part in parts))


def cert_names(extra: Optional[list[str]] = None) -> list[str]:
    """What the certificate is made out to: this PC's names and addresses (Home Assistant pins the
    fingerprint and does not look at them; a browser does)."""
    import socket

    names = ["localhost", "127.0.0.1"]
    try:
        host = socket.gethostname()
        names += [host, host.split(".")[0] + ".local"]
    except OSError:
        pass
    try:
        from .discovery import lan_addresses

        names += lan_addresses("0.0.0.0")
    except Exception:  # noqa: BLE001
        pass
    names += list(extra or [])
    seen: list[str] = []
    for name in names:
        name = "".join(ch for ch in str(name).strip() if ch.isalnum() or ch in ".-:_")
        if name and name.lower() not in [s.lower() for s in seen]:
            seen.append(name)
    return seen


def _make_cert_cryptography(names: list[str], days: int) -> Optional[tuple[str, str]]:
    """``(certificate PEM, key PEM)`` with the ``cryptography`` package, or None when it is not installed."""
    try:
        import datetime as dt
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return None
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PC Link")])
    alt: list[Any] = []
    for name in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False).sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()).decode("ascii"))


def _make_cert_powershell(names: list[str], days: int) -> tuple[str, str]:
    """The same with Windows' own means (.NET through PowerShell); raises when that does not work."""
    import base64
    import json
    import subprocess

    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    env = dict(os.environ, PCLINK_CERT_CN="PC Link", PCLINK_CERT_NAMES="|".join(names), PCLINK_CERT_DAYS=str(int(days)))
    proc = subprocess.run(
        [os.path.join(system32, "WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive",
         "-Command", _CERT_SCRIPT],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120, cwd=system32, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()[:400] or f"exit code {proc.returncode}")
    data = json.loads(proc.stdout.decode("utf-8", "replace"))
    key = rsa_private_key_der([base64.b64decode(data[k]) for k in ("n", "e", "d", "p", "q", "dp", "dq", "qi")])
    return _pem("CERTIFICATE", base64.b64decode(data["cert"])), _pem("RSA PRIVATE KEY", key)


def make_cert(folder: str, names: list[str], days: int = 3650) -> tuple[str, str, str]:
    """Write a self-signed certificate and its key into ``folder``; ``(cert path, key path, fingerprint)``."""
    import ssl

    from .api import cert_fingerprint

    pair = _make_cert_cryptography(names, days)
    if pair is None:
        if sys.platform != "win32":
            raise RuntimeError("the 'cryptography' package is needed for this (pip install cryptography)")
        pair = _make_cert_powershell(names, days)
    cert_path, key_path = os.path.join(folder, CERT_FILE), os.path.join(folder, KEY_FILE)
    for path, text in ((key_path, pair[1]), (cert_path, pair[0])):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="ascii", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_path, key_path)   # raises unless the two fit
    return cert_path, key_path, cert_fingerprint(cert_path) or ""


def cmd_make_cert(argv: list[str]) -> int:
    """Create the certificate for ``api.tls_cert`` / ``api.tls_key`` in the data folder."""
    parser = argparse.ArgumentParser(prog=f"{cli_name()} make-cert")
    parser.add_argument("--force", action="store_true", help="replace an existing certificate (Home Assistant "
                        "must then be given the new fingerprint)")
    parser.add_argument("--days", type=int, default=3650)
    parser.add_argument("--name", action="append", default=[], help="another host name or address to put in it")
    parser.add_argument("--dir", default=None, help="write the two files here instead of the data folder")
    args = parser.parse_args(argv)
    folder = args.dir or data_dir()
    if not args.dir:
        # the key must not be readable by the users of the PC
        from .hardening import DataDirError, ensure_data_dir

        try:
            ensure_data_dir(folder)
        except DataDirError as exc:
            print(f"not writing a key: {exc}", file=sys.stderr)
            return 3
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(os.path.join(folder, CERT_FILE)) and not args.force:
        from .api import cert_fingerprint

        print(f"{os.path.join(folder, CERT_FILE)} already exists (use --force to replace it)")
        print(f"fingerprint (SHA-256): {cert_fingerprint(os.path.join(folder, CERT_FILE))}")
        return 1
    try:
        cert_path, key_path, fingerprint = make_cert(folder, cert_names(args.name), max(1, args.days))
    except Exception as exc:  # noqa: BLE001
        print(f"could not create the certificate: {exc}", file=sys.stderr)
        return 1
    print("# paste into the api: section of config.yaml (then restart the service):")
    print(f"  tls_cert: '{cert_path}'")
    print(f"  tls_key: '{key_path}'")
    print("")
    print("# Home Assistant then asks for this fingerprint (it is also announced on the network, so")
    print("# a PC that is discovered automatically needs nothing typed in):")
    print(f"fingerprint (SHA-256): {fingerprint}")
    return 0


# -- setup-api: the API, its token and HTTPS in one go ---------------------------------------------------

_TOP_KEY = re.compile(r"^api:[ \t]*(?P<rest>[^#\r\n]*?)[ \t]*(?:#[^\r\n]*)?$")
_KEY_LINE = re.compile(r"^(?P<indent>[ \t]+)(?P<hash>#[ \t]*)?(?P<key>[A-Za-z_]+):(?P<value>[^\r\n]*?)"
                       r"(?P<comment>[ \t]+#[^\r\n]*)?$")


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def set_api_section(text: str, values: dict[str, Any]) -> str:
    """``text`` (a config.yaml) with these keys of its ``api:`` section set (pure).

    A key that is there is given the new value and keeps its comment; one that is only
    there as a commented-out example is switched on in its place; any other is added at
    the end of the section.  Everything else in the file stays as it was.  A file
    without an ``api:`` section gets one at the end."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    start = next((i for i, line in enumerate(lines) if _TOP_KEY.match(line)), None)
    if start is None:
        block = ["api:"] + [f"  {k}: {_yaml_scalar(v)}" for k, v in values.items()]
        body = text if not text or text.endswith(newline) else text + newline
        return body + newline.join(block) + newline
    flow = _TOP_KEY.match(lines[start]).group("rest").strip()
    if flow:   # api: {enabled: true, ...} - rewritten as a block (that line's comment goes)
        import yaml

        current = yaml.safe_load(flow)
        if not isinstance(current, dict):
            raise ValueError("the api: section of the configuration is not a mapping")
        merged = {**current, **values}
        lines[start:start + 1] = ["api:"] + [f"  {k}: {_yaml_scalar(v) if k in values else json_scalar(v)}"
                                             for k, v in merged.items()]
        return newline.join(lines)
    end = start + 1   # the section: the indented (or blank) lines that follow
    last = start      # its last indented line
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
        if lines[end].strip():
            last = end
        end += 1
    indent = "  "
    for i in range(start + 1, end):
        m = _KEY_LINE.match(lines[i])
        if m and not m.group("hash"):
            indent = m.group("indent")
            break
    todo = dict(values)
    for want_commented in (False, True):   # a real key first; an example line only when there is none
        for i in range(start + 1, end):
            m = _KEY_LINE.match(lines[i])
            if not m or m.group("key") not in todo or bool(m.group("hash")) != want_commented:
                continue
            if m.group("indent") != indent:
                continue   # a deeper level (a list or mapping value): not a key of api:
            comment = m.group("comment") or ""
            lines[i] = f"{indent}{m.group('key')}: {_yaml_scalar(todo.pop(m.group('key')))}{comment}"
    added = [f"{indent}{k}: {_yaml_scalar(v)}" for k, v in todo.items()]
    lines[last + 1:last + 1] = added
    return newline.join(lines)


def json_scalar(value: Any) -> str:
    """A value of a flow-style mapping written back as YAML (JSON is valid YAML)."""
    import json

    return json.dumps(value)


def _usable_pair(cert: Optional[str], key: Optional[str]) -> bool:
    """Do these two files exist and belong together?"""
    if not cert or not key or not (os.path.isfile(cert) and os.path.isfile(key)):
        return False
    import ssl

    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        return True
    except (ssl.SSLError, OSError, ValueError):
        return False


def _restart_service_if_running() -> Optional[bool]:
    """Restart the installed service so it takes the new API settings: True when done, None when it
    is not installed or not running (it reads the settings when it starts), False when it failed."""
    if sys.platform != "win32":
        return None
    try:
        import win32service
        import win32serviceutil

        status = win32serviceutil.QueryServiceStatus("PCLink")[1]
    except Exception as exc:  # noqa: BLE001
        return None if getattr(exc, "winerror", None) == 1060 else False   # 1060: no such service
    if status != win32service.SERVICE_RUNNING:
        return None
    try:
        win32serviceutil.RestartService("PCLink")
        return True
    except Exception:  # noqa: BLE001 - not allowed (no administrator prompt)
        return False


def cmd_setup_api(argv: list[str]) -> int:
    """Everything the Home Assistant integration needs, written straight into config.yaml: the API
    on for the LAN, a token (the one there is kept) and HTTPS with a certificate of this PC's own."""
    import secrets

    parser = argparse.ArgumentParser(prog=f"{cli_name()} setup-api")
    parser.add_argument("--config", default=None)
    parser.add_argument("--new-token", action="store_true", help="replace the token (Home Assistant must then "
                        "be given the new one)")
    parser.add_argument("--new-cert", action="store_true", help="replace the certificate (Home Assistant must "
                        "then be given the new fingerprint)")
    parser.add_argument("--no-tls", action="store_true", help="leave HTTPS off")
    parser.add_argument("--no-restart", action="store_true", help="do not restart a running service")
    parser.add_argument("--show", action="store_true", help="only show what Home Assistant needs; change nothing")
    args = parser.parse_args(argv)
    path = args.config or default_config_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        cfg = Config.from_yaml(text)
    except FileNotFoundError:
        print(f"config not found: {path}\nrun '{cli_name()} init-config' first", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"config cannot be read: {exc} (run this from an administrator prompt)", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"config error: {exc}\nfix it first ('{cli_name()} check-config')", file=sys.stderr)
        return 2
    api = cfg.api
    if args.show:
        loopback = api.host in ("127.0.0.1", "localhost", "::1")
        if not (api.enabled and api.token and not loopback):
            print(f"The API is not set up for Home Assistant yet: run '{cli_name()} setup-api' (as administrator).")
            return 1
        from .api import cert_fingerprint

        tls = bool(api.tls_cert and api.tls_key)
        _print_pairing(api, api.token, cert_fingerprint(api.tls_cert) if tls else None, tls)
        return 0
    values: dict[str, Any] = {"enabled": True}
    if api.host in ("127.0.0.1", "localhost", "::1"):
        values["host"] = "0.0.0.0"   # reachable from Home Assistant
    token = api.token if api.token and not args.new_token else secrets.token_urlsafe(24)
    values["token"] = token
    fingerprint = None
    if not args.no_tls:
        from .api import cert_fingerprint

        folder = os.path.dirname(os.path.abspath(path))
        cert, key = api.tls_cert, api.tls_key
        if not _usable_pair(cert, key):
            cert, key = os.path.join(folder, CERT_FILE), os.path.join(folder, KEY_FILE)
        if args.new_cert or not _usable_pair(cert, key):
            if os.path.normcase(folder) == os.path.normcase(os.path.abspath(data_dir())):
                from .hardening import DataDirError, ensure_data_dir   # the key must not be readable by the users

                try:
                    ensure_data_dir(folder)
                except DataDirError as exc:
                    print(f"not writing a key: {exc}", file=sys.stderr)
                    return 3
            try:
                cert, key, _fp = make_cert(folder, cert_names())
            except Exception as exc:  # noqa: BLE001
                print(f"could not create the certificate: {exc}", file=sys.stderr)
                return 1
        values["tls_cert"], values["tls_key"] = cert, key
        fingerprint = cert_fingerprint(cert)
    new_text = set_api_section(text, values)
    try:
        new_cfg = Config.from_yaml(new_text)
    except ConfigError as exc:   # never leave a configuration behind that the service would refuse
        print(f"the configuration could not be updated ({exc}); edit the api: section by hand", file=sys.stderr)
        return 1
    if not (new_cfg.api.enabled and new_cfg.api.token == token
            and (args.no_tls or (new_cfg.api.tls_cert, new_cfg.api.tls_key) == (values["tls_cert"], values["tls_key"]))):
        print("the configuration could not be updated; edit the api: section by hand", file=sys.stderr)
        return 1
    changed = new_text != text
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        os.replace(tmp, path)
    restarted = None if (not changed or args.no_restart) else _restart_service_if_running()
    print(f"{'updated' if changed else 'already set up:'} {path}")
    if restarted:
        print("  the PC Link service was restarted to apply it")
    elif restarted is False:
        print(f"  restart the PC Link service to apply it ('{cli_name()} restart')")
    _print_pairing(new_cfg.api, token, fingerprint, not args.no_tls)
    return 0


def _print_pairing(api: Any, token: str, fingerprint: Optional[str], tls: bool) -> None:
    """What to enter in Home Assistant."""
    print(f"  API: {'https' if tls else 'http'}://{api.host}:{api.port}/")
    print("")
    print("In Home Assistant: Settings -> Devices & services. This PC shows up under 'Discovered'")
    print("(or: Add integration -> PC Link). Enter this token:")
    print("")
    print(f"    {token}")
    if fingerprint:
        print("")
        print("Only when adding it by hand, also tick HTTPS and enter the fingerprint (SHA-256):")
        print(f"    {fingerprint}")
    elif not tls:
        print("")
        print("HTTPS is off: the token crosses the network in the clear. To turn it on, run (as administrator)")
        print(f"    {cli_name()} setup-api")
        print("and then, in Home Assistant, Reconfigure the PC with HTTPS ticked and the fingerprint it prints.")
    print(f"\nShow this again at any time: {cli_name()} setup-api --show")


def cmd_api_info(argv: list[str]) -> int:
    """How the local API is configured, as one line of JSON (for the installers' firewall rule)."""
    import json

    try:
        cfg = Config.load(_config_arg(argv))
    except Exception as exc:  # noqa: BLE001 - missing, unreadable, invalid
        print(json.dumps({"error": str(exc)[:300]}))
        return 2
    api = cfg.api
    loopback = api.host in ("127.0.0.1", "localhost", "::1")
    print(json.dumps({"enabled": bool(api.enabled), "host": api.host, "port": int(api.port),
                      "lan": bool(api.enabled and not loopback),
                      "tls": bool(getattr(api, "tls_cert", None) and getattr(api, "tls_key", None))}))
    return 0


# name -> (import to try, required everywhere, required on Windows, what it enables)
SELFTEST_COMPONENTS: dict[str, tuple[str, bool, bool, str]] = {
    "psutil": ("psutil", True, True, "CPU, memory, disk and network metrics"),
    "pyyaml": ("yaml", True, True, "configuration file"),
    "pywin32": ("win32api", False, True, "service, sessions, logon blocking, tray icon"),
    "servicemanager": ("servicemanager", False, True, "running as a Windows service"),
    "comtypes": ("comtypes.client", False, False, "browser URL, audio activity, volume control"),
    "uia": ("<uia>", False, False, "reading the active browser tab (UI Automation)"),
    "zeroconf": ("zeroconf", False, False, "Home Assistant auto-discovery (mDNS)"),
    "tzdata": ("tzdata", False, False, "time_limits.time_zone (a fixed time zone for day boundaries)"),
    "nvml": ("pynvml", False, False, "NVIDIA GPU metrics"),
    "pythonnet": ("clr", False, False, "in-process LibreHardwareMonitor sensors"),
    "pillow": ("PIL.Image", False, False, "JPEG screenshots (PNG works without it)"),
    "now-playing": ("winrt.windows.media.control", False, False, "song / video title from Windows' media controls"),
    "agent-exe": ("<agent-exe>", False, False, "per-session agent executable (standalone build)"),
}


def _selftest_probe(target: str) -> None:
    """Import / exercise one component; raises when it is unavailable."""
    import importlib

    if target == "<uia>":
        if sys.platform != "win32":
            raise RuntimeError("Windows only")
        import comtypes.client  # type: ignore

        comtypes.client.GetModule("UIAutomationCore.dll")  # generates the typelib wrapper
        importlib.import_module("comtypes.gen.UIAutomationClient")
    elif target == "<agent-exe>":
        if not is_frozen():
            raise RuntimeError("only part of the standalone build")
        path = os.path.join(app_dir(), AGENT_EXE)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
    else:
        importlib.import_module(target)


def cmd_selftest(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{cli_name()} selftest")
    parser.add_argument("--require", default="", help="comma-separated components that must be available")
    args = parser.parse_args(argv)
    required = {r.strip() for r in args.require.split(",") if r.strip()}
    unknown = required - set(SELFTEST_COMPONENTS)
    if unknown:
        print(f"unknown component(s): {', '.join(sorted(unknown))}; known: {', '.join(SELFTEST_COMPONENTS)}")
        return 2
    windows = sys.platform == "win32"
    print(f"home-assistant-pc-link {__version__} ({'standalone build' if is_frozen() else 'Python ' + sys.version.split()[0]})")
    print(f"executable: {sys.executable}")
    failed = []
    for name, (target, core, core_windows, purpose) in SELFTEST_COMPONENTS.items():
        try:
            _selftest_probe(target)
            state = "ok"
        except Exception as exc:  # noqa: BLE001
            state = f"missing ({type(exc).__name__}: {str(exc)[:80]})"
            if name in required or core or (windows and core_windows):
                failed.append(name)
        print(f"  {name:<15} {state:<45} {purpose}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.format(version=__version__, cli=cli_name()))
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
    if cmd == SERVICE_HOST_ARG:
        from .service import run_service_host

        return run_service_host()
    if cmd == "selftest":
        return cmd_selftest(rest)
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
    if cmd == "api-token":
        return cmd_api_token(rest)
    if cmd == "setup-api":
        return cmd_setup_api(rest)
    if cmd == "make-cert":
        return cmd_make_cert(rest)
    if cmd == "api-info":
        return cmd_api_info(rest)
    print(USAGE.format(version=__version__, cli=cli_name()))
    return 1


if __name__ == "__main__":
    sys.exit(main())
