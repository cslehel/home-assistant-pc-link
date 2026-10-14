"""Updating PC Link itself from its GitHub releases.

A background thread asks GitHub for the latest release every few hours and
publishes "installed / latest" so Home Assistant can show an *update* entity.
Installing (a press in Home Assistant, or automatically with
``updates.auto_install``) downloads the release's Windows zip, checks it,
unpacks it into the administrators-only data folder and runs the zip's own
``install.ps1`` - the same upgrade path as running the installer by hand.  The
installer stops the service, replaces the files and starts the new version.

Only the standalone build can replace itself; a Python install reports the
available version but has to be updated with git / pip.

Trust: the download comes over HTTPS from the configured GitHub repository
only, its SHA-256 must match the digest GitHub publishes for the release
asset, and the archive is unpacked without following paths out of the staging
folder.  The package says itself which version it is (its ``VERSION`` file,
which the signed catalog covers): that must be the announced version and newer
than the installed one, so an old release cannot be handed out under a new
name.  Its code signature is checked before anything from it runs (see
``pclink/codesign.py``): a signed copy only accepts an update signed by the
same publisher, whose signed catalog must list exactly the files unpacked.
An unsigned copy has no such anchor: the service runs as SYSTEM, so the
repository named in ``updates.repository`` is then trusted completely - leave
it at the default unless you publish your own builds.

The installer swaps the program folder and puts the old one back when the new
version does not start; it writes what happened to ``install-result.json`` in
the data folder, which is read here at the next start (and while waiting), so
a failed update is reported instead of staying "installing" for ever.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import __version__, codesign
from .config import UpdatesConfig
from .runtime import app_dir, is_frozen

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/repos/{repo}/releases/latest"
API_URL_ALL = "https://api.github.com/repos/{repo}/releases?per_page=10"
ASSET_RE = re.compile(r"^PCLink-.+-windows-x64\.zip$", re.I)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DOWNLOAD_HOSTS = ("github.com", "githubusercontent.com")
MAX_METADATA = 2 * 1024 * 1024
MAX_DOWNLOAD = 400 * 1024 * 1024
MAX_UNPACKED = 1500 * 1024 * 1024
TIMEOUT = 30                          # seconds one network read may wait
METADATA_DEADLINE = 120               # seconds a release lookup may take in all
DOWNLOAD_DEADLINE = 45 * 60           # seconds the download may take in all (a server that sends a byte now and then
                                      # would otherwise hold it, and with it the update, for ever)
SUMMARY_CHARS = 255  # Home Assistant's limit for an update entity's release summary
NOTES_CHARS = 4000   # of the release notes kept for Home Assistant's "release notes" view
VERSION_FILE = "VERSION"             # in the package: the version it is, one line (covered by the signed catalog)
RESULT_FILE = "install-result.json"   # in the data folder: what the installer did last (written by install.ps1)
INSTALL_LOG = "update-install.log"    # in the data folder: the installer's output of the last update
INSTALL_TIMEOUT = 20 * 60             # seconds after which an installer that has not replaced this service has failed
INSTALL_POLL = 15                     # seconds between looks at the installer's result while one is running


def parse_version(text: Any) -> tuple[int, ...]:
    """``v0.4.1`` -> ``(0, 4, 1)``; anything after the numbers (rc1, +build) is ignored."""
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(text or ""))
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def is_newer(candidate: Any, installed: Any) -> bool:
    a, b = parse_version(candidate), parse_version(installed)
    if not a or not b:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def package_version(package: str) -> str:
    """What the unpacked package says its version is (``""`` when it does not say)."""
    try:
        with open(os.path.join(package, VERSION_FILE), "r", encoding="utf-8-sig") as fh:
            lines = fh.read(200).strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return lines[0].strip() if lines else ""


def check_package_version(found: str, announced: Any, installed: Any) -> None:
    """Raise unless the package is the announced version and newer than the installed one."""
    if not found or not parse_version(found):
        raise ValueError(f"the download does not say which version it is (no {VERSION_FILE} file)")
    if found.lstrip("vV").lower() != str(announced or "").strip().lstrip("vV").lower():
        raise ValueError(f"the download is version {found}, not the announced {announced}")
    if not is_newer(found, installed):
        raise ValueError(f"the download is version {found}, which is not newer than the installed {installed}")


def read_install_result(path: Optional[str]) -> Optional[dict[str, Any]]:
    """What ``install.ps1`` wrote about its last run, or None."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:   # Windows PowerShell writes a byte order mark
            data = json.loads(fh.read(MAX_METADATA))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "ok" not in data:
        return None
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return {"ok": bool(data.get("ok")), "version": str(data.get("version") or "")[:40],
            "previous": str(data.get("previous") or "")[:40], "error": (str(data.get("error") or "")[:500] or None),
            "rolled_back": bool(data.get("rolled_back")), "ts": ts}


def _allowed_download(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in DOWNLOAD_HOSTS)


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_download(newurl):
            raise urllib.error.URLError(f"refusing redirect to {urllib.parse.urlsplit(newurl).netloc}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            # the token is for api.github.com only; the storage host a download redirects to must not see it
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def _open(url: str, accept: str, token: Optional[str] = None):
    headers = {"User-Agent": f"pclink/{__version__}", "Accept": accept}
    if token and (urllib.parse.urlsplit(url).hostname or "").lower() == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.build_opener(_HttpsOnlyRedirects).open(request, timeout=TIMEOUT)


def _chunks(resp, seconds: float, size: int = 256 * 1024):
    """The body of ``resp`` piece by piece, all of it within ``seconds``.

    Every piece is one read from the network (``read1``), which waits at most ``TIMEOUT``:
    ``read(n)`` would go on reading until it had ``n`` bytes, and a server that sends a byte
    now and then never runs into the socket's timeout."""
    deadline = time.monotonic() + seconds
    read = getattr(resp, "read1", None) or resp.read
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"the transfer took longer than {int(seconds)} s")
        chunk = read(size)
        if not chunk:
            return
        yield chunk


def pick_release(releases: list[dict[str, Any]], include_prereleases: bool) -> Optional[dict[str, Any]]:
    """The newest usable release: published, with a Windows zip attached."""
    best: Optional[dict[str, Any]] = None
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft") or (rel.get("prerelease") and not include_prereleases):
            continue
        if not parse_version(rel.get("tag_name")):
            continue
        if best is None or is_newer(rel.get("tag_name"), best.get("tag_name")):
            best = rel
    return best


def release_asset(release: dict[str, Any]) -> Optional[dict[str, Any]]:
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and ASSET_RE.match(str(asset.get("name") or "")):
            return asset
    return None


def safe_extract(archive: str, target: str) -> None:
    """Unpack ``archive`` into ``target``; refuse entries that would land outside it."""
    root = os.path.realpath(target)
    with zipfile.ZipFile(archive) as zf:
        total = 0
        for info in zf.infolist():
            total += info.file_size
            if total > MAX_UNPACKED:
                raise ValueError("archive is too large when unpacked")
            name = info.filename.replace("\\", "/")
            dest = os.path.realpath(os.path.join(root, name))
            if name.startswith("/") or ":" in name or not (dest == root or dest.startswith(root + os.sep)):
                raise ValueError(f"archive entry outside the target folder: {info.filename!r}")
        zf.extractall(root)


class SelfUpdate:
    """Checks for, and installs, new PC Link releases."""

    def __init__(self, cfg: UpdatesConfig, staging_root: str, on_change: Optional[Callable[[str], None]] = None,
                 installed: str = __version__, can_install: Optional[bool] = None,
                 fetch: Optional[Callable[[str], Any]] = None,
                 signatures: Optional[Callable[[str], codesign.PackageSignatures]] = None,
                 installed_signature: Optional[Callable[[], codesign.Signature]] = None):
        self.cfg = cfg
        self.staging_root = staging_root
        self.installed = installed
        # next to the staging folder, that is in the administrators-only data folder
        folder = os.path.dirname(staging_root)
        self.record_path = os.path.join(folder, codesign.RECORD_FILE) if folder else None
        self.result_path = os.path.join(folder, RESULT_FILE) if folder else None
        self.install_log = os.path.join(folder, INSTALL_LOG) if folder else None
        self._installed_signature = installed_signature or self._read_installed_signature
        self._on_change = on_change
        self._fetch = fetch or self._fetch_json
        self._signatures = signatures or self._read_signatures
        self._signature_note: Optional[str] = None   # what the last signature check found (under _lock)
        self._can_install = (is_frozen() and sys.platform == "win32") if can_install is None else can_install
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._check_requested = False
        self._install_requested = False
        # state (under _lock)
        self._latest: Optional[str] = None
        self._release_url: Optional[str] = None
        self._summary: Optional[str] = None
        self._asset: Optional[dict[str, Any]] = None
        self._status = "idle"   # idle | checking | downloading | installing | error
        self._error: Optional[str] = None
        self._last_check: Optional[float] = None
        self._announced: Optional[str] = None
        self._notes: Optional[str] = None                     # the release's notes (the body on GitHub, cut)
        self._percentage: Optional[int] = None                # how far the download is, while there is one
        self._last_install: Optional[dict[str, Any]] = None   # the installer's last result (install-result.json)
        self._failed_version: Optional[str] = None            # not installed again without being asked
        self._installing_since: Optional[float] = None        # time.time() when the installer was started

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        self._cleanup_staging()
        self._adopt_install_result()
        self._thread = threading.Thread(target=self._loop, name="self-update", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- API ---------------------------------------------------------------------------
    @property
    def installable(self) -> bool:
        """Can this copy replace itself, and is that allowed?"""
        return bool(self._can_install and self.cfg.allow_install)

    def check_now(self) -> None:
        self._check_requested = True
        self._wake.set()

    def install(self) -> tuple[bool, str]:
        """Start installing the latest release in the background.  ``(accepted, why not)``."""
        if not self.cfg.allow_install:
            return False, "installing updates is switched off (updates.allow_install)"
        if not self._can_install:
            return False, "only the standalone Windows build can update itself; update a Python install with git/pip"
        with self._lock:
            if self._status in ("downloading", "installing"):
                return True, ""
            if not self._latest or not is_newer(self._latest, self.installed):
                return False, "no newer version is known; check for updates first"
            if self._asset is None:
                return False, f"release {self._latest} has no Windows download"
            self._install_requested = True
        self._wake.set()
        return True, ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latest = self._latest or self.installed
            available = bool(self._latest and is_newer(self._latest, self.installed))
            return {
                "pclink_installed_version": self.installed,
                "pclink_latest_version": latest,
                "pclink_update_available": available,
                "pclink_update_in_progress": self._status in ("downloading", "installing"),
                "pclink_update_release_url": self._release_url,
                "pclink_update_summary": self._summary,
                "pclink_update_release_notes": self._notes,
                "pclink_update_percentage": self._percentage if self._status == "downloading" else None,
                "pclink_update_info": {
                    "status": self._status,
                    "error": self._error,
                    "last_check": (datetime.fromtimestamp(self._last_check, timezone.utc).isoformat(timespec="seconds")
                                   if self._last_check else None),
                    "can_install": self.installable,
                    "auto_install": bool(self.cfg.auto_install),
                    "repository": self.cfg.repository,
                    "signature": self._signature_note,
                    "last_install": self._last_install_public(),
                },
            }

    # -- worker ------------------------------------------------------------------------
    def _loop(self) -> None:
        if self._can_install:
            try:
                self._installed_signer()   # write down who signed this copy while nothing depends on it yet
            except Exception:  # noqa: BLE001
                log.exception("could not record the installed version's signer")
        self._stop.wait(60)  # let the service settle (and the network come up) after boot
        next_check = 0.0
        while not self._stop.is_set():
            if self._watch_install():
                self._wake.wait(INSTALL_POLL)
                self._wake.clear()
                continue
            now = time.monotonic()
            if self._check_requested or (self.cfg.enabled and now >= next_check):
                self._check_requested = False
                self._check()
                next_check = time.monotonic() + max(1.0, float(self.cfg.check_interval_hours)) * 3600
            with self._lock:
                wanted = self._install_requested
                self._install_requested = False
                auto = bool(self.cfg.auto_install and self.installable and self._asset is not None and self._latest
                            and is_newer(self._latest, self.installed) and self._status == "idle"
                            and self._latest != self._failed_version)   # a version that failed is not tried in a loop
            if wanted or auto:
                self._install()
            self._wake.wait(300)
            self._wake.clear()

    def _set(self, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            self._status, self._error = status, error
            if status != "installing":
                self._installing_since = None

    # -- what the installer reported ---------------------------------------------------
    def _last_install_public(self) -> Optional[dict[str, Any]]:
        last = self._last_install
        if not last:
            return None
        return {"ok": last["ok"], "version": last["version"], "error": last["error"], "rolled_back": last["rolled_back"],
                "finished": (datetime.fromtimestamp(last["ts"], timezone.utc).isoformat(timespec="seconds")
                             if last["ts"] else None)}

    @staticmethod
    def _failure_text(result: dict[str, Any]) -> str:
        text = f"update to {result['version'] or '?'} failed: {result['error'] or 'the installer reported a failure'}"
        return text + ("; the previous version was put back" if result["rolled_back"] else "")

    def _adopt_install_result(self) -> None:
        """At start: take over what the installer wrote, so that a failed update is shown, not forgotten."""
        result = read_install_result(self.result_path)
        if result is None:
            return
        with self._lock:
            self._last_install = result
            failed = not result["ok"] and (not parse_version(result["version"]) or is_newer(result["version"], self.installed))
            if failed:
                self._failed_version = result["version"].lstrip("vV") or None
                self._status, self._error = "error", self._failure_text(result)
        if failed:
            log.error("the last %s", self._failure_text(result))
        elif result["ok"]:
            log.info("the installer reported: version %s installed", result["version"] or "?")

    def _watch_install(self) -> bool:
        """While an installer runs: has it reported, or run out of time?  True while still waiting.

        A successful installer replaces this very process, so whatever is seen here
        is a failure: either it says so, or it never finished."""
        with self._lock:
            since = self._installing_since
            if self._status != "installing" or since is None:
                return False
        result = read_install_result(self.result_path)
        if result is not None and result["ts"] >= since - 5:
            with self._lock:
                self._last_install = result
                self._installing_since = None
                if result["ok"]:   # it finished, but this service was not restarted by it
                    self._status, self._error = "idle", None
                else:
                    self._failed_version = result["version"].lstrip("vV") or self._latest
                    self._status, self._error = "error", self._failure_text(result)
            if not result["ok"]:
                log.error("the %s", self._failure_text(result))
            return False
        if time.time() - since > INSTALL_TIMEOUT:
            with self._lock:
                self._failed_version = self._latest
                self._installing_since = None
                self._status = "error"
                self._error = (f"update to {self._latest} failed: the installer did not finish within "
                               f"{INSTALL_TIMEOUT // 60} minutes" + (f" (see {self.install_log})" if self.install_log else ""))
            log.error("%s", self._error)
            return False
        return True

    def _fetch_json(self, url: str) -> Any:
        body = bytearray()
        with _open(url, "application/vnd.github+json", self.cfg.github_token) as resp:
            for chunk in _chunks(resp, METADATA_DEADLINE, 64 * 1024):
                body += chunk
                if len(body) > MAX_METADATA:
                    break
        return json.loads(bytes(body[:MAX_METADATA]).decode("utf-8"))

    def _check(self) -> None:
        repo = str(self.cfg.repository or "")
        if not REPO_RE.match(repo):
            self._set("error", f"updates.repository {repo!r} is not of the form owner/name")
            return
        with self._lock:
            if self._status in ("downloading", "installing"):
                return
            self._status = "checking"
        try:
            if self.cfg.include_prereleases:
                data = self._fetch(API_URL_ALL.format(repo=repo))
                release = pick_release(data if isinstance(data, list) else [], True)
            else:
                data = self._fetch(API_URL.format(repo=repo))
                release = pick_release([data] if isinstance(data, dict) else [], False)
        except Exception as exc:  # noqa: BLE001 - offline, rate limited, bad JSON: try again next time
            reason = str(exc)
            if getattr(exc, "code", None) == 404:
                reason = (f"{repo} has no published release, or it is a private repository "
                          "(then set updates.github_token)")
            elif getattr(exc, "code", None) in (401, 403):
                reason = f"GitHub refused the request ({exc}); check updates.github_token or try again later"
            log.info("update check failed: %s", reason)
            with self._lock:
                self._status, self._error, self._last_check = "error", f"check failed: {reason}", time.time()
            return
        announce = None
        with self._lock:
            self._last_check = time.time()
            self._status, self._error = "idle", None
            if release is not None:
                self._latest = str(release.get("tag_name") or "").lstrip("vV") or None
                url = str(release.get("html_url") or "")
                self._release_url = url if _allowed_download(url) else None
                body = " ".join(str(release.get("body") or "").split())
                self._summary = (body[: SUMMARY_CHARS - 1] + "…") if len(body) > SUMMARY_CHARS else (body or None)
                notes = str(release.get("body") or "").strip()
                self._notes = ((notes[: NOTES_CHARS - 1] + "…") if len(notes) > NOTES_CHARS else notes) or None
                self._asset = release_asset(release)
                if self._latest and is_newer(self._latest, self.installed) and self._announced != self._latest:
                    self._announced = announce = self._latest
        if announce:
            log.info("PC Link %s is available (installed: %s)", announce, self.installed)
            if self._on_change:
                try:
                    self._on_change("pclink_update_available")
                except Exception:  # noqa: BLE001
                    log.debug("update notification failed", exc_info=True)

    def _cleanup_staging(self) -> None:
        try:
            if os.path.isdir(self.staging_root) and not os.path.islink(self.staging_root):
                shutil.rmtree(self.staging_root, ignore_errors=True)
        except OSError:
            pass

    def _download(self, asset: dict[str, Any], target: str) -> None:
        token = self.cfg.github_token
        # a private repository serves its files only through the API, with the token
        url = str((asset.get("url") if token else None) or asset.get("browser_download_url") or "")
        if not _allowed_download(url):
            raise ValueError("the release download is not on GitHub")
        digest = str(asset.get("digest") or "")
        if not digest.lower().startswith("sha256:"):
            raise ValueError("GitHub published no SHA-256 for this download; install it by hand")
        sha = hashlib.sha256()
        size = 0
        try:
            total = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            total = 0
        self._percentage = 0 if total > 0 else None
        with _open(url, "application/octet-stream", token) as resp, open(target, "wb") as fh:
            for chunk in _chunks(resp, DOWNLOAD_DEADLINE):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError("the download is larger than expected")
                sha.update(chunk)
                fh.write(chunk)
                if total > 0:
                    self._percentage = min(100, size * 100 // total)
        if sha.hexdigest().lower() != digest.split(":", 1)[1].strip().lower():
            raise ValueError("the download does not match the checksum GitHub published")

    def _install(self) -> None:
        with self._lock:
            asset, version = self._asset, self._latest
        if asset is None or not version or not is_newer(version, self.installed):
            return
        log.warning("updating PC Link %s -> %s", self.installed, version)
        self._set("downloading")
        stage = os.path.join(self.staging_root, re.sub(r"[^0-9A-Za-z.]", "_", version))
        try:
            self._cleanup_staging()
            os.makedirs(stage)
            archive = os.path.join(stage, "download.zip")
            self._download(asset, archive)
            safe_extract(archive, stage)
            os.remove(archive)
            package = os.path.join(stage, "PCLink")
            installer = os.path.join(package, "install.ps1")
            if not (os.path.isfile(installer) and os.path.isfile(os.path.join(package, "pclink.exe"))):
                raise ValueError("the download is not a PC Link package")
            check_package_version(package_version(package), version, self.installed)
            self._check_signature(package)
            with self._lock:
                self._status, self._error, self._installing_since = "installing", None, time.time()
            self._run_installer(installer, stage)
        except Exception as exc:  # noqa: BLE001
            log.error("update to %s failed: %s", version, exc)
            self._set("error", f"update to {version} failed: {exc}")
            self._cleanup_staging()

    @staticmethod
    def _read_installed_signature() -> codesign.Signature:
        """What the running executable's signature says right now (a Python install has none)."""
        return codesign.read_signature(sys.executable) if is_frozen() else codesign.Signature("unsigned")

    def _installed_signer(self) -> codesign.Signature:
        """Who signed this installation: the record in the data folder, kept up to date (``codesign.anchor``)."""
        live = self._installed_signature()
        record = codesign.load_record(self.record_path) if self.record_path else None
        signer, changed = codesign.anchor(record, live)
        if changed and self.record_path:
            if record is not None and record.valid and not codesign.same_publisher(live, record):
                log.warning("the installed version is now signed by %s (%s); it was %s (%s)",
                            live.signer, live.thumbprint, record.signer, record.thumbprint)
            if codesign.save_record(self.record_path, signer, self.installed):
                log.info("recorded the installed version's signer: %s",
                         f"{signer.signer} ({signer.thumbprint})" if signer.valid else "not signed")
        return signer

    def _read_signatures(self, package: str) -> codesign.PackageSignatures:
        found = codesign.read_package(package)
        found.installed = self._installed_signer()
        return found

    def _check_signature(self, package: str) -> None:
        """Refuse the unpacked update unless its code signature is acceptable (``codesign.judge``)."""
        ok, note = codesign.judge(self._signatures(package), bool(self.cfg.require_signature),
                                  list(self.cfg.trusted_signers or []))
        with self._lock:
            self._signature_note = note
        if not ok:
            raise ValueError(note)
        log.info("update package: %s", note)

    def _run_installer(self, installer: str, stage: str) -> None:
        """Start the package's installer detached: it stops this service, replaces the files and starts it again."""
        system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        powershell = os.path.join(system32, "WindowsPowerShell", "v1.0", "powershell.exe")
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
        command = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", installer,
                   "-InstallDir", app_dir(), "-NoPause"]
        # the log lives outside the staging folder, which the next start of the service clears
        out = open(self.install_log or os.path.join(stage, "install.log"), "wb")  # noqa: SIM115 - handed to the child
        try:
            try:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=system32,
                                 creationflags=flags, close_fds=True)
            except OSError:  # in a job that does not allow breaking away: start it inside the job
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=system32,
                                 creationflags=flags & ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), close_fds=True)
        finally:
            out.close()
        log.warning("installer started; the service will restart")
