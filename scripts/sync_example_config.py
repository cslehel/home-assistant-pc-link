"""Copy config.example.yaml into pclink/example_config.py (the embedded fallback).

Run after editing the example config:  python scripts/sync_example_config.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML = os.path.join(ROOT, "config.example.yaml")
PY = os.path.join(ROOT, "pclink", "example_config.py")


def main() -> int:
    with open(YAML, "r", encoding="utf-8") as fh:
        yaml_text = fh.read()
    # The text is embedded as a raw string between ''' and ''': it must not contain the
    # delimiter, end in a quote (which would run into it) or end in a backslash (which would escape it).
    if "'''" in yaml_text:
        print("config.example.yaml must not contain ''' (used as the embedding delimiter)", file=sys.stderr)
        return 1
    if yaml_text.endswith(("'", "\\")):
        print("config.example.yaml must not end in a quote or a backslash; end it with a newline", file=sys.stderr)
        return 1
    with open(PY, "r", encoding="utf-8") as fh:
        source = fh.read()
    new, count = re.subn(r"EMBEDDED = r'''.*?'''", lambda m: "EMBEDDED = r'''" + yaml_text + "'''", source, flags=re.S)
    if count != 1:
        # Nothing to replace is not "in sync": the marker was renamed or reformatted.
        print(f"{PY}: expected exactly one EMBEDDED = r'''...''' block, found {count}; nothing written", file=sys.stderr)
        return 1
    if new == source:
        print("already in sync")
        return 0
    with open(PY, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"updated {PY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
