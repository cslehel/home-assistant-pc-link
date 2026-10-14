"""Logging setup shared by the service, the console runner and the agent."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys


def setup_logging(path: str, level: str = "INFO", filename: str = "pclink.log", max_bytes: int = 2_000_000,
                  backups: int = 5, console: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        os.makedirs(path, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(os.path.join(path, filename), maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        console = True
        print(f"cannot open log file in {path}: {exc}", file=sys.stderr)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
