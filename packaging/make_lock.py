"""Write packaging/requirements-lock.txt: the exact versions (with hashes) the Windows build installs.

    python packaging/make_lock.py

The build (.github/workflows/build.yml) installs from the lock file with
``pip install --require-hashes`` when it exists, so that a release is built
from the same packages every time and a changed or replaced package on PyPI
cannot slip into it.  Run this again to take newer versions, look at the
difference, and commit it.

pip resolves ``packaging/requirements-build.txt`` for the build's interpreter
(CPython 3.12 on 64-bit Windows, wheels only) without installing anything; the
result is taken from its installation report.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_VERSION = "3.12"   # keep in step with python-version in .github/workflows/build.yml
PLATFORM = "win_amd64"

HEADER = f"""# Exact versions of everything the standalone Windows build installs, with hashes.
# Made by packaging/make_lock.py for CPython {PYTHON_VERSION} on {PLATFORM}; do not edit by hand.
# Installed by the build with: pip install --require-hashes -r packaging/requirements-lock.txt
"""


def lock_lines(report: dict[str, Any]) -> list[str]:
    """``name==version --hash=sha256:...`` for every package in a pip installation report (pure)."""
    lines = []
    for item in report.get("install") or []:
        meta = item.get("metadata") or {}
        hashes = ((item.get("download_info") or {}).get("archive_info") or {}).get("hashes") or {}
        name, version, digest = meta.get("name"), meta.get("version"), hashes.get("sha256")
        if not (name and version and digest):
            raise ValueError(f"no version or SHA-256 for {name or item!r} in pip's report")
        lines.append(f"{name}=={version} \\\n    --hash=sha256:{digest}")
    return sorted(lines, key=str.lower)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        report_path = os.path.join(tmp, "report.json")
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
               "--report", report_path, "--python-version", PYTHON_VERSION, "--platform", PLATFORM,
               "--implementation", "cp", "--only-binary=:all:", "--target", os.path.join(tmp, "unused"),
               "-r", os.path.join(HERE, "requirements-build.txt")]
        done = subprocess.run(cmd, check=False)
        if done.returncode != 0:
            print("pip could not resolve the build requirements (see above)", file=sys.stderr)
            return 1
        with open(report_path, "r", encoding="utf-8") as fh:
            lines = lock_lines(json.load(fh))
    target = os.path.join(HERE, "requirements-lock.txt")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(HEADER + "\n".join(lines) + "\n")
    print(f"wrote {target}: {len(lines)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
