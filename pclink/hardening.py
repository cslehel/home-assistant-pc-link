"""Tamper resistance for the service.

Goal: a *monitored* user (a standard, non-administrator account -- the correct
setup for time limits) must not be able to stop, pause, disable or delete the
service to escape their time limit.  Administrators keep full control so the
software can always be reconfigured, stopped and uninstalled.

This is done with the two mechanisms Windows provides for exactly this, the
same ones parental-control products use:

* a restrictive service security descriptor (DACL) that grants ordinary and
  interactive users read/query access only -- no SERVICE_STOP, SERVICE_PAUSE,
  SERVICE_CHANGE_CONFIG or DELETE;
* automatic restart on unexpected termination (failure/recovery actions).

What this deliberately does NOT do, because it is how malware behaves, would
also make antivirus flag the program, and would leave the owner unable to
remove their own software: it does not mark the process critical (that
blue-screens the machine on kill), does not block Task Manager or the Services
console, and does not hide the process.  The real security boundary on Windows
is the standard-user vs. administrator line: keep the monitored account a
standard user.  An administrator can always stop any service, by design.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

# Service-specific access rights (see sddl / winsvc.h):
#   CC SERVICE_QUERY_CONFIG      DC SERVICE_CHANGE_CONFIG   LC SERVICE_QUERY_STATUS
#   SW SERVICE_ENUMERATE_DEPEND  RP SERVICE_START           WP SERVICE_STOP
#   DT SERVICE_PAUSE_CONTINUE    LO SERVICE_INTERROGATE     CR SERVICE_USER_DEFINED_CONTROL
#   RC READ_CONTROL  SD DELETE   WD WRITE_DAC               WO WRITE_OWNER
#
# Administrators (BA) & LocalSystem (SY): full control incl. stop/delete/reconfigure.
# Interactive (IU) & Service (SU) & Authenticated users (AU): query/read only -- no stop.
# Audit stop/config/delete attempts (SACL) so tampering attempts are logged.
HARDENED_SDDL = (
    "D:"
    "(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"   # Administrators: full
    "(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;SY)"   # LocalSystem: full
    "(A;;CCLCSWLOCRRC;;;IU)"                  # Interactive users: read/query
    "(A;;CCLCSWLOCRRC;;;SU)"                  # Service logons: read/query
    "(A;;CCLCSWLOCRRC;;;AU)"                  # Authenticated users: read/query
    "S:"
    "(AU;FA;WPDTDCSD;;;WD)"                   # audit stop/pause/reconfigure/delete by anyone
)

# Windows' own default descriptor for a newly created service.  Note that this
# ALREADY denies interactive/standard users SERVICE_STOP: on Windows only
# administrators can stop services by default.  Hardening therefore mainly
# (a) re-asserts that even if something loosened it, (b) adds auditing and
# (c) turns on auto-restart.  ``soften_acl`` restores exactly this default.
DEFAULT_SDDL = (
    "D:"
    "(A;;CCLCSWRPWPDTLOCRRC;;;SY)"
    "(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
    "(A;;CCLCSWLOCRRC;;;IU)"
    "(A;;CCLCSWLOCRRC;;;SU)"
    "S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)"
)

# DACL-only variant used when the SACL part cannot be written (needs SeSecurityPrivilege).
HARDENED_SDDL_DACL_ONLY = HARDENED_SDDL.split("S:")[0]


def _sc(*args: str) -> subprocess.CompletedProcess:
    sc = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "sc.exe")
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([sc, *args], capture_output=True, text=True, timeout=20, creationflags=creation)


def set_recovery(service_name: str, delay_ms: int = 5000, reset_seconds: int = 86400) -> bool:
    """Restart the service automatically if it exits unexpectedly."""
    if sys.platform != "win32":
        return False
    actions = f"restart/{delay_ms}/restart/{delay_ms}/restart/{delay_ms}"
    res = _sc("failure", service_name, f"reset={reset_seconds}", f"actions={actions}")
    if res.returncode != 0:
        log.warning("could not set recovery actions: %s %s", res.stdout.strip(), res.stderr.strip())
        return False
    # Also restart on a clean but unexpected non-zero stop.
    _sc("failureflag", service_name, "1")
    log.info("recovery actions set: auto-restart after %d ms", delay_ms)
    return True


def harden_acl(service_name: str) -> bool:
    """Remove stop/pause/reconfigure/delete rights from non-administrators."""
    if sys.platform != "win32":
        return False
    res = _sc("sdset", service_name, HARDENED_SDDL)
    if res.returncode != 0:
        # Writing the audit (SACL) part needs SeSecurityPrivilege; fall back to the DACL alone.
        res = _sc("sdset", service_name, HARDENED_SDDL_DACL_ONLY)
        if res.returncode != 0:
            log.warning("could not harden service ACL: %s %s", res.stdout.strip(), res.stderr.strip())
            return False
        log.info("service ACL hardened (DACL only; auditing not applied) for %s", service_name)
        return True
    log.info("service ACL hardened: only administrators can stop or reconfigure %s", service_name)
    return True


def soften_acl(service_name: str) -> bool:
    """Restore the default ACL (call before uninstalling if the ACL was hardened)."""
    if sys.platform != "win32":
        return False
    res = _sc("sdset", service_name, DEFAULT_SDDL)
    if res.returncode != 0:
        log.warning("could not restore service ACL: %s %s", res.stdout.strip(), res.stderr.strip())
        return False
    log.info("service ACL restored to default for %s", service_name)
    return True


def show_acl(service_name: str) -> str:
    if sys.platform != "win32":
        return ""
    return _sc("sdshow", service_name).stdout.strip()


def apply_all(service_name: str, harden: bool = True) -> None:
    set_recovery(service_name)
    if harden:
        harden_acl(service_name)
