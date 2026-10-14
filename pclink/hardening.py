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
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

log =logging.getLogger(__name__)

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


def apply_all(service_name: str, harden: bool = True) -> dict[str, bool]:
    """Recovery actions and (unless switched off) the ACL.  Returns what worked, by name."""
    done = {"auto-restart": set_recovery(service_name)}
    if harden:
        done["service permissions"] = harden_acl(service_name)
    return done


# ---------------------------------------------------------------------------
# data folder (%ProgramData%\PCLink): configuration with secrets, database, logs
# ---------------------------------------------------------------------------

_SID_SYSTEM = "*S-1-5-18"
_SID_ADMINS = "*S-1-5-32-544"
_REPARSE_POINT = 0x400


def _icacls(*args: str) -> subprocess.CompletedProcess:
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=60, creationflags=creation)


def _is_reparse_point(path: str) -> bool:
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return False


class DataDirError(Exception):
    """The data folder cannot be used: it is a link, or it could not be restricted to administrators."""


_TRUSTED_OWNERS = {"S-1-5-18", "S-1-5-32-544"}   # LocalSystem, Administrators
QUARANTINE_PREFIX = "quarantine-"                  # where secure_data_dir moves what it did not write


def _owner_sid(item: str) -> Optional[str]:
    """The owner of ``item`` as a SID string; ``"?"`` when it cannot be read; None without pywin32."""
    try:
        import win32security  # type: ignore
    except ImportError:
        return None
    try:
        sd = win32security.GetFileSecurity(item, win32security.OWNER_SECURITY_INFORMATION)
        return win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
    except Exception:  # noqa: BLE001 - unreadable: say so rather than hide it
        return "?"


def _own_sid() -> Optional[str]:
    """The account this process runs as (SYSTEM for the service, the administrator for the installer)."""
    try:
        import win32api  # type: ignore
        import win32security  # type: ignore

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    except Exception:  # noqa: BLE001 - pywin32 missing or the token unreadable: trust only the built-in accounts
        return None


def _admin_sids() -> set[str]:
    """The accounts in the local Administrators group (empty when that cannot be read)."""
    try:
        import win32net  # type: ignore
        import win32security  # type: ignore

        name, _domain, _kind = win32security.LookupAccountSid(None, win32security.ConvertStringSidToSid("S-1-5-32-544"))
        found: set[str] = set()
        resume = 0
        while True:
            members, _total, resume = win32net.NetLocalGroupGetMembers(None, name, 0, resume)
            found.update(win32security.ConvertSidToStringSid(m["sid"]) for m in members)
            if not resume:
                return found
    except Exception:  # noqa: BLE001
        return set()


def _trusted_owners() -> set[str]:
    """Who may have written what is in the data folder: LocalSystem, Administrators, the
    members of that group (an elevated editor may leave its user as the owner) and this process.
    A monitored user is a standard user, so none of these."""
    own = _own_sid()
    return _TRUSTED_OWNERS | _admin_sids() | ({own} if own else set())


def is_restricted(path: str) -> Optional[bool]:
    """Has ``path`` been restricted already (by :func:`secure_data_dir`)?  Owned by Administrators
    or LocalSystem, inheritance cut, and nobody else in its ACL.  None without pywin32.

    The owner matters: whoever created the folder may change its ACL at any time, so one that a
    standard user created and locked down to look "restricted" is still theirs."""
    try:
        import win32security  # type: ignore
    except ImportError:
        return None
    try:
        info = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
        sd = win32security.GetFileSecurity(path, info)
        if win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner()) not in _TRUSTED_OWNERS:
            return False
        control, _revision = sd.GetSecurityDescriptorControl()
        if not control & win32security.SE_DACL_PROTECTED:
            return False
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:   # a NULL DACL: everybody
            return False
        for i in range(dacl.GetAceCount()):
            if win32security.ConvertSidToStringSid(dacl.GetAce(i)[-1]) not in _TRUSTED_OWNERS:
                return False
        return True
    except Exception:  # noqa: BLE001 - what cannot be read is not known to be safe
        return False


def foreign_owned(path: str, limit: int = 20) -> list[str]:
    """Entries in ``path`` (and the folder itself) that belong to neither Administrators nor LocalSystem.

    Something a standard user put there before PC Link was installed: a database with
    settings of their choosing, say.  Best effort (needs pywin32); never raises."""
    if _owner_sid(path) is None:
        return []
    trusted = _TRUSTED_OWNERS
    found: list[str] = []

    def check(item: str) -> None:
        if _owner_sid(item) not in trusted and len(found) < limit:
            found.append(item)

    try:
        check(path)
        for root, dirs, files in os.walk(path):
            names = dirs + files   # a junction is listed (it is somebody's), but not looked into
            dirs[:] = [d for d in dirs if not _is_reparse_point(os.path.join(root, d))]
            for name in names:
                check(os.path.join(root, name))
                if len(found) >= limit:
                    return found
    except OSError:
        pass
    return found


def _link_count(item: str) -> int:
    """How many names the file has (more than one: a hard link); 2 when that cannot be read."""
    try:
        return os.lstat(item).st_nlink
    except OSError:
        return 2


def _strangers(path: str, trusted: set[str]) -> list[str]:
    """What a folder that is secured for the first time must not keep: entries owned by another
    account (or whose owner cannot be read), links and junctions, and files with more than one
    name (a hard link to a file elsewhere, whose permissions would be reset with ours).  A
    foreign folder is listed as a whole, not looked into; quarantine folders are left alone."""
    found: list[str] = []
    for root, dirs, files in os.walk(path):
        keep = []
        for name in sorted(dirs) + sorted(files):
            item = os.path.join(root, name)
            if root == path and name.startswith(QUARANTINE_PREFIX):
                continue
            foreign = _is_reparse_point(item) or _owner_sid(item) not in trusted \
                or (name in files and _link_count(item) > 1)
            if foreign:
                found.append(item)
            elif name in dirs:
                keep.append(name)
        dirs[:] = keep
    return found


def _quarantine(path: str, items: list[str]) -> Optional[str]:
    """Move ``items`` (inside ``path``) into a new ``quarantine-<time>`` folder in ``path``, keeping
    their relative paths.  Returns that folder; raises OSError when anything could not be moved."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = os.path.join(path, QUARANTINE_PREFIX + stamp)
    n = 1
    while os.path.lexists(target):
        n += 1
        target = os.path.join(path, f"{QUARANTINE_PREFIX}{stamp}-{n}")
    os.mkdir(target)   # inherits the restricted ACL of the data folder (restricted before this)
    for item in items:
        dest = os.path.join(target, os.path.relpath(item, path))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(item, dest)   # a link is moved, never what it points to
    return target


def secure_data_dir(path: str) -> bool:
    """Make the data folder readable and writable by administrators and LocalSystem only.

    A folder created under ``%ProgramData%`` inherits read access (and the right to
    create files) for every user, which would let a monitored user read the API
    tokens and the parent PIN from ``config.yaml``.  Ownership is
    taken as well, so something a standard user created there beforehand cannot be
    re-opened by its former owner.  Links and junctions are never followed.

    When the folder is secured for the first time (it was not restricted yet, see
    :func:`is_restricted`), what an administrator did not write is not adopted: entries
    owned by another account, links and hard-linked files are moved into a
    ``quarantine-<time>`` folder inside it (restricted like the rest) for an administrator
    to look at.  When that is not possible the folder is not used.

    False means the folder is NOT safe to keep secrets in; callers must act on that
    (see :func:`ensure_data_dir`).
    """
    if sys.platform != "win32" or not path or not os.path.isdir(path):
        return False
    if _is_reparse_point(path):
        log.error("data folder %s is a link or junction; refusing to use it for secrets", path)
        return False
    first = not is_restricted(path)
    trusted = _trusted_owners()
    if first and _owner_sid(path) is None and os.listdir(path):
        log.error("data folder %s is not restricted yet and has content, but who owns it cannot be read "
                  "(pywin32 missing); refusing to use it", path)
        return False
    if not first:
        strangers = foreign_owned(path)
        if strangers:
            # taken over below, but an administrator should know: these were not written by PC Link
            log.warning("data folder %s holds entries owned by another account (check them): %s",
                        path, ", ".join(strangers))
    ok = True
    try:
        res = _icacls(path, "/setowner", _SID_ADMINS, "/L", "/Q")
        ok &= res.returncode == 0
        res = _icacls(path, "/inheritance:r", "/grant:r", f"{_SID_SYSTEM}:(OI)(CI)F", f"{_SID_ADMINS}:(OI)(CI)F",
                      "/L", "/Q")
        if res.returncode != 0:
            log.warning("could not restrict access to %s: %s %s", path, res.stdout.strip(), res.stderr.strip())
            return False
        if first:
            # The folder is closed to everybody else now; what somebody put there before goes aside.
            strangers = _strangers(path, trusted)
            if strangers:
                try:
                    where = _quarantine(path, strangers)
                except OSError as exc:
                    log.error("data folder %s holds entries PC Link did not write and they could not be moved "
                              "aside (%s); refusing to use it: %s", path, exc, ", ".join(strangers))
                    return False
                log.error("data folder %s held entries that an administrator did not write (a monitored user may "
                          "have put them there); moved to %s, not used: %s", path, where, ", ".join(strangers))
        # everything already inside: take ownership and fall back to the (now restricted) inherited ACL
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not _is_reparse_point(os.path.join(root, d))]
            for name in dirs + files:
                item = os.path.join(root, name)
                if _is_reparse_point(item):
                    continue
                if name in files and _link_count(item) > 1:
                    # another name of a file elsewhere: its permissions are not ours to change
                    log.warning("data folder: %s is a hard link; left as it is", item)
                    continue
                ok &= _icacls(item, "/setowner", _SID_ADMINS, "/L", "/Q").returncode == 0
                ok &= _icacls(item, "/reset", "/L", "/Q").returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not restrict access to %s: %s", path, exc)
        return False
    if ok:
        log.info("data folder %s restricted to administrators and SYSTEM", path)
    else:
        log.warning("data folder %s: some entries could not be re-secured", path)
    return ok


def data_dir_problem(path: str) -> str:
    """Why ``path`` must not be used as the data folder as it stands (``""``: nothing known against it)."""
    if sys.platform != "win32" or not path:
        return ""
    if _is_reparse_point(path):
        return (f"{path} is a link or junction to another folder. Somebody created it before PC Link was installed; "
                "delete it (rmdir) from an administrator prompt and install again")
    if os.path.exists(path) and not os.path.isdir(path):
        return f"{path} is a file, not a folder; delete it from an administrator prompt and install again"
    return ""


def ensure_data_dir(path: str) -> None:
    """Create the data folder if needed and restrict it; raise :class:`DataDirError` when that fails.

    The installer, ``init-config``, ``harden`` and the start of the service all go
    through here and all stop on an error.  For the service that means no time
    limits until an administrator has looked at it, which is deliberate: a data
    folder that a monitored user controls (a junction planted before the first
    install is the known way) hands that user the configuration, and with its
    ``commands:`` the whole PC as SYSTEM - worse than a service that visibly
    does not run.  Once the folder has been restricted a standard user cannot
    get it back into such a state, so this cannot be used to switch the limits
    off from a child's account.  Nothing to do outside Windows.
    """
    if sys.platform != "win32" or not path:
        return
    problem = data_dir_problem(path)
    if problem:
        raise DataDirError(problem)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise DataDirError(f"{path} could not be created: {exc}") from exc
    if _is_reparse_point(path):   # looked at again: it must be the folder that was just checked or made
        raise DataDirError(data_dir_problem(path))
    if not secure_data_dir(path):
        raise DataDirError(f"access to {path} could not be restricted to administrators (run this from an "
                           "administrator prompt; details are in the log)")


# ---------------------------------------------------------------------------
# a configuration that stopped loading must not switch the limits off
# ---------------------------------------------------------------------------

LAST_GOOD_CONFIG = "config.last-good.yaml"   # in the data folder: a copy of the last configuration that loaded


def remember_good_config(config_path: str, folder: str) -> bool:
    """Keep a copy of a configuration file that just loaded (best effort; never raises)."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=folder)
        os.close(fd)
        shutil.copyfile(config_path, tmp)
        os.replace(tmp, os.path.join(folder, LAST_GOOD_CONFIG))
        return True
    except OSError as exc:
        log.debug("could not keep a copy of the working configuration: %s", exc)
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def load_config_or_last_good(config_path: str, folder: str) -> tuple[Any, Optional[str]]:
    """Load ``config_path``; when it does not load, the last configuration that did.

    Returns ``(config, problem)``: ``problem`` is None normally, and says what is
    wrong with the file when the copy was used instead.  A typing error in
    ``config.yaml`` then costs the last edit, not the time limits.  The copy is
    used as if it were the file itself (``source_path`` stays the real path), so
    correcting the file is picked up like any other change.  With no copy to fall
    back on the original error is raised.
    """
    from .config import Config

    try:
        cfg = Config.load(config_path)
    except Exception as exc:  # noqa: BLE001 - not there, not YAML, not valid: all the same here
        fallback = os.path.join(folder, LAST_GOOD_CONFIG)
        if not os.path.isfile(fallback):
            raise
        try:
            cfg = Config.load(fallback)
        except Exception:  # noqa: BLE001 - the copy is no better: report the real file's problem
            raise exc from None
        cfg.source_path = config_path
        return cfg, f"{config_path} could not be loaded ({exc}); running with the last configuration that worked"
    remember_good_config(config_path, folder)
    return cfg, None
