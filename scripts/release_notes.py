"""Print the CHANGELOG.md section for one version (used as GitHub release notes).

Usage: python scripts/release_notes.py v0.3.0
Exits with an error when the changelog has no section for that version, so a
release cannot be published without notes.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def section(changelog: str, version: str) -> str:
    version = version.lstrip("v")
    heading = re.compile(r"^## \[(?P<v>[^\]]+)\]", re.M)
    matches = list(heading.finditer(changelog))
    for i, m in enumerate(matches):
        if m.group("v") == version:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
            body = changelog[m.end():end]
            body = body.split("\n", 1)[1] if "\n" in body else ""  # drop the rest of the heading line (date)
            return body.strip() + "\n"
    raise LookupError(f"CHANGELOG.md has no section for version {version}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
        text = fh.read()
    try:
        notes = section(text, argv[1])
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not notes.strip():
        print(f"error: the CHANGELOG.md section for {argv[1]} is empty", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
