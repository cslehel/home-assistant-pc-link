"""Code signatures of a downloaded update (Authenticode, Windows).

Two things are read with Windows' own tools (``Get-AuthenticodeSignature`` and
``Test-FileCatalog``, through PowerShell, which the update needs anyway):

* the signature of the package's executables, of its installer script
  (``install.ps1``, which the update runs as SYSTEM) and of its **catalog**
  (``pclink.cat``) - a signed list of the hash of every file in the package,
  made by the release build when a certificate is configured;
* whether the unpacked files are exactly the ones that catalog lists.

:func:`judge` then decides.  The rule that needs no configuration: a copy of
PC Link that is signed only ever accepts an update signed by the same
publisher.  ``updates.trusted_signers`` pins the certificate instead, and
``updates.require_signature`` refuses unsigned updates outright.  The
executables and the installer are signed themselves; the bundled runtime next
to them (Python and its libraries, other publishers' files) cannot be, which
is why a signed update must also carry the catalog.

Who "the same publisher" is does not depend on what can be read at the moment
of the update: the signer of the installed copy is written down once
(:func:`save_record`, ``signer.json`` in the administrators-only data folder)
and only ever replaced by another valid signer.  Everything fails closed: what
could not be read is ``unknown``, and ``unknown`` is a refusal.

Everything here is pure except :func:`read_package`, :func:`read_signature`
and the two functions that read and write the record.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

EXECUTABLES = ("pclink.exe", "pclink-agent.exe")
INSTALLER = "install.ps1"
CATALOG = "pclink.cat"
SIGNED = EXECUTABLES + (INSTALLER, CATALOG)   # what a signed update must carry, each with the publisher's signature
RECORD_FILE = "signer.json"                   # in the data folder: who signed the installed copy
TIMEOUT = 180

# No double quotes in here: the script travels as one command-line argument.  Every file
# and the catalog are read inside their own try/catch, so that one unreadable item (a
# catalog that is not a catalog, say) is reported as such instead of losing all the others.
_SCRIPT = (
    # the answer in UTF-8: otherwise the console's OEM code page mangles any non-ASCII path or name
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
    "$ErrorActionPreference = 'Stop'; "
    "$files = @(foreach ($p in ($env:PCLINK_SIG_FILES -split '\\|')) { if ($p) { "
    "try { $s = Get-AuthenticodeSignature -LiteralPath $p; $c = $s.SignerCertificate; $root = ''; "
    "$ca = ''; $caSubject = ''; $caKey = ''; "
    "if ($c) { $chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain; "
    "$chain.ChainPolicy.RevocationMode = 'NoCheck'; [void]$chain.Build($c); "
    "if ($chain.ChainElements.Count -gt 0) { "
    "$root = [string]$chain.ChainElements[$chain.ChainElements.Count - 1].Certificate.Thumbprint }; "
    "if ($chain.ChainElements.Count -gt 1) { $i = $chain.ChainElements[1].Certificate; "
    "$ca = [string]$i.Thumbprint; $caSubject = [string]$i.Subject; "
    "$caKey = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($i.GetPublicKey()))) -replace '-', '' } }; "
    "[pscustomobject]@{ path = $p; status = [string]$s.Status; thumbprint = [string]$c.Thumbprint; "
    "subject = [string]$c.Subject; issuer = [string]$c.Issuer; root = $root; ca = $ca; ca_subject = $caSubject; "
    "ca_key = $caKey; message = [string]$s.StatusMessage } } "
    "catch { [pscustomobject]@{ path = $p; status = ''; thumbprint = ''; subject = ''; issuer = ''; root = ''; "
    "ca = ''; ca_subject = ''; ca_key = ''; message = [string]$_.Exception.Message } } } }); "
    "$catalog = $null; "
    "if ($env:PCLINK_SIG_CATALOG) { try { $catalog = [string](Test-FileCatalog -CatalogFilePath $env:PCLINK_SIG_CATALOG "
    "-Path $env:PCLINK_SIG_FOLDER -Detailed -FilesToSkip (Split-Path -Leaf $env:PCLINK_SIG_CATALOG)).Status } "
    "catch { $catalog = 'Unreadable' } }; "
    "[pscustomobject]@{ files = $files; catalog = $catalog } | ConvertTo-Json -Depth 4 -Compress"
)


@dataclass(frozen=True)
class Signature:
    status: str = "unknown"    # valid | unsigned | invalid | unknown (could not be checked)
    thumbprint: str = ""
    subject: str = ""
    detail: str = ""
    issuer: str = ""           # who issued the signing certificate
    root: str = ""             # thumbprint of the root certificate its chain ends in
    ca: str = ""               # thumbprint of the certificate that issued the signing one (the intermediate CA)
    ca_subject: str = ""       # that certificate's subject ...
    ca_key: str = ""           # ... and the SHA-256 of its public key (a re-issued CA certificate keeps both)

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    @property
    def signer(self) -> str:
        """The publisher's name for messages (the certificate's CN), else its thumbprint."""
        for part in self.subject.split(","):
            if part.strip().upper().startswith("CN="):
                return part.strip()[3:]
        return self.subject or self.thumbprint or "?"


@dataclass
class PackageSignatures:
    """What was read from an unpacked update."""

    files: dict[str, Signature] = field(default_factory=dict)   # lower-case file name -> signature
    catalog_matches: Optional[bool] = None    # the files are the ones the catalog lists (None: no catalog, not checked)
    # Who signed the installed copy: the recorded signer, see :func:`anchor`.  ``unsigned``
    # says "this installation never was signed"; ``unknown`` that it could not be found out.
    installed: Signature = field(default_factory=lambda: Signature("unsigned"))
    error: Optional[str] = None               # the signatures could not be read at all


def _signature(status: Any, thumbprint: Any = "", subject: Any = "", message: Any = "", issuer: Any = "",
               root: Any = "", ca: Any = "", ca_subject: Any = "", ca_key: Any = "") -> Signature:
    text = str(status or "")
    kind = {"valid": "valid", "notsigned": "unsigned", "": "unknown"}.get(text.lower(), "invalid")
    return Signature(kind, str(thumbprint or "").upper(), str(subject or ""), str(message or text)[:300],
                     str(issuer or ""), str(root or "").upper(), str(ca or "").upper(), str(ca_subject or ""),
                     str(ca_key or "").upper())


def same_issuing_ca(sig: Signature, known: Signature) -> bool:
    """Was ``sig``'s certificate issued by the same CA certificate as ``known``'s?  The same
    certificate (thumbprint), or one with the same subject and the same public key (a CA
    certificate that was re-issued)."""
    if sig.ca and sig.ca == known.ca:
        return True
    return bool(sig.ca_subject and sig.ca_key and sig.ca_subject == known.ca_subject and sig.ca_key == known.ca_key)


def same_publisher(sig: Signature, known: Signature) -> bool:
    """Is ``sig`` made by the publisher ``known`` stands for?

    The same certificate, or - certificates are renewed every year or so - one
    with the same subject, issued by the same intermediate CA certificate (see
    :func:`same_issuing_ca`) and whose chain ends in the same root certificate.
    The subject alone is not enough, nor is the root: a public root is shared by
    every customer of that certificate authority, and any certificate authority
    Windows trusts could issue a certificate with the same name.  A self-signed
    certificate is its own root, so a new one never matches the old one; a
    renewal at another issuing CA does not match either: pin those with
    ``updates.trusted_signers``."""
    if sig.thumbprint and sig.thumbprint == known.thumbprint:
        return True
    return bool(sig.subject and sig.subject == known.subject and sig.root and sig.root == known.root
                and same_issuing_ca(sig, known))


def _run(paths: list[str], catalog: str = "", folder: str = "") -> dict[str, Any]:
    """Run the PowerShell reader; raises when it produced nothing usable."""
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    env = dict(os.environ, PCLINK_SIG_FILES="|".join(paths), PCLINK_SIG_FOLDER=folder, PCLINK_SIG_CATALOG=catalog)
    proc = subprocess.run(
        [os.path.join(system32, "WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive",
         "-Command", _SCRIPT],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT, cwd=system32, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    data = json.loads(proc.stdout.decode("utf-8-sig", "replace").strip() or "null")
    if proc.returncode != 0 or not isinstance(data, dict):
        raise ValueError((proc.stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}")[:300])
    return data


def _by_path(data: dict[str, Any]) -> dict[str, Signature]:
    found: dict[str, Signature] = {}
    for item in data.get("files") or []:
        if isinstance(item, dict):
            found[os.path.normcase(str(item.get("path")))] = _signature(
                item.get("status"), item.get("thumbprint"), item.get("subject"), item.get("message"),
                item.get("issuer"), item.get("root"), item.get("ca"), item.get("ca_subject"), item.get("ca_key"))
    return found


def read_signature(path: Optional[str]) -> Signature:
    """The signature of one file.  Never raises: ``unknown`` when it could not be read."""
    if sys.platform != "win32" or not path or not os.path.isfile(path):
        return Signature()
    try:
        return _by_path(_run([path])).get(os.path.normcase(path), Signature())
    except Exception as exc:  # noqa: BLE001 - no PowerShell, a timeout, unreadable output
        log.warning("the signature of %s could not be read: %s", path, exc)
        return Signature(detail=str(exc)[:300])


def read_package(folder: str, installed_exe: Optional[str] = None) -> PackageSignatures:
    """Read the signatures of the package unpacked in ``folder`` (and of ``installed_exe``).

    Never raises.  What could not be read is ``unknown`` (one file) or ``error``
    (nothing at all), and :func:`judge` refuses an update on either.  The installed
    executable is read on its own, so that nothing in the download can get in its way."""
    result = PackageSignatures()
    if installed_exe:
        result.installed = read_signature(installed_exe)
    if sys.platform != "win32":
        result.error = "code signatures can only be read on Windows"
        return result
    paths = {name: os.path.join(folder, name) for name in SIGNED}
    present = {name: path for name, path in paths.items() if os.path.isfile(path)}
    try:
        data = _run(list(present.values()), present.get(CATALOG, ""), folder)
    except Exception as exc:  # noqa: BLE001 - no PowerShell, a timeout, unreadable output
        log.warning("the update's signatures could not be read: %s", exc)
        result.error = str(exc)[:300]
        return result
    by_path = _by_path(data)
    for name, path in present.items():
        result.files[name] = by_path.get(os.path.normcase(path), Signature())
    if CATALOG in present:
        # no answer about a catalog that is there is a "no"
        result.catalog_matches = str(data.get("catalog") or "").lower() == "valid"
    return result


# -- the recorded signer of the installed copy ---------------------------------------------------------

def load_record(path: str) -> Optional[Signature]:
    """The signer written down for this installation, or None when there is no (readable) record."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") not in ("valid", "unsigned"):
        return None
    return Signature(str(data["status"]), str(data.get("thumbprint") or "").upper(), str(data.get("subject") or ""),
                     "", str(data.get("issuer") or ""), str(data.get("root") or "").upper(),
                     str(data.get("ca") or "").upper(), str(data.get("ca_subject") or ""),
                     str(data.get("ca_key") or "").upper())


def save_record(path: str, sig: Signature, version: str = "") -> bool:
    data = {"status": sig.status, "thumbprint": sig.thumbprint, "subject": sig.subject, "issuer": sig.issuer,
            "root": sig.root, "ca": sig.ca, "ca_subject": sig.ca_subject, "ca_key": sig.ca_key,
            "version": version, "recorded": int(time.time())}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="signer-", suffix=".tmp", dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def anchor(record: Optional[Signature], live: Signature) -> tuple[Signature, bool]:
    """Who signed the installed copy, from the record and from what its executable says now.

    Returns ``(signer, the record must be rewritten)``.  The record only ever
    moves towards "signed": a valid signature on the installed executable always
    counts (only an administrator or a checked update can have put it there),
    but an executable that reads as unsigned, broken or unreadable never takes a
    recorded signer away.  Without a record, an unsigned executable starts one
    ("never was signed"); one that cannot be read leaves the answer ``unknown``."""
    if live.valid:
        changed = (record is None or not record.valid or record.thumbprint != live.thumbprint or record.root != live.root
                   or (record.ca, record.ca_subject, record.ca_key) != (live.ca, live.ca_subject, live.ca_key))
        return live, changed
    if record is not None:
        return record, False
    if live.status == "unsigned":
        return live, True
    return Signature(detail=live.detail), False


def judge(found: PackageSignatures, require: bool = False, trusted: Optional[list[str]] = None) -> tuple[bool, str]:
    """May this update be installed?  Returns ``(yes, what was found or why not)``."""
    trusted = [t.upper() for t in (trusted or [])]
    if found.error:
        return False, f"the update's signatures could not be read ({found.error})"
    for name, sig in found.files.items():
        if sig.status == "invalid":
            return False, f"the signature of {name} is not valid ({sig.detail or 'it does not match the file'})"
    for name, sig in found.files.items():
        if sig.status == "unknown":
            return False, f"the signature of {name} could not be checked"
    if found.catalog_matches is False:
        return False, f"the files in the update are not the ones its catalog ({CATALOG}) lists"
    if trusted:
        wanted = "a certificate listed in updates.trusted_signers"

        def accepted(sig: Signature) -> bool:
            return sig.thumbprint in trusted
    elif found.installed.valid:
        wanted = f"{found.installed.signer}, who signed the installed version"

        def accepted(sig: Signature) -> bool:
            return same_publisher(sig, found.installed)
    elif found.installed.status != "unsigned":
        return False, ("it could not be found out who signed the installed version"
                       + (f" ({found.installed.detail})" if found.installed.detail else ""))
    elif require:
        wanted = "a publisher this PC trusts (updates.require_signature)"

        def accepted(sig: Signature) -> bool:
            return True
    else:
        for name in (EXECUTABLES[0], INSTALLER):   # the two files every package has: no answer about them is a "no"
            if name not in found.files:
                return False, f"the signature of {name} could not be checked"
        signed = sorted({sig.signer for sig in found.files.values() if sig.valid})
        return True, f"signed by {', '.join(signed)}" if signed else "not signed (no signature is required)"
    for name in SIGNED:
        sig = found.files.get(name)
        if sig is None:
            what = "it carries no signed catalog" if name == CATALOG else f"{name} is missing"
            return False, f"the update must be signed by {wanted}, but {what}"
        if not sig.valid:
            return False, f"the update must be signed by {wanted}, but {name} is not signed"
        if not accepted(sig):
            return False, f"the update must be signed by {wanted}, but {name} is signed by {sig.signer} ({sig.thumbprint})"
    if found.catalog_matches is not True:
        return False, f"the update's files could not be checked against its catalog ({CATALOG})"
    return True, f"signed by {found.files[CATALOG].signer}; every file matches the signed catalog"
