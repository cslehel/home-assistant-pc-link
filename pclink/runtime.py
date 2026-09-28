"""How this copy of PC Link is running: from a Python install or as the standalone build.

The standalone build (PyInstaller, see ``packaging/pclink.spec``) ships two
executables in one folder:

* ``pclink.exe``        - command line, and the Windows service host
                          (the service manager starts ``pclink.exe service``);
* ``pclink-agent.exe``  - the per-session agent (windowed: no console window).
"""
from __future__ import annotations

import os
import sys
from typing import Optional

AGENT_EXE = "pclink-agent.exe"
SERVICE_HOST_ARG = "service"


def is_frozen() -> bool:
    """True when running from the standalone build."""
    return bool(getattr(sys, "frozen", False))


def cli_name() -> str:
    """How the user invokes the command line, for help and error messages."""
    return "pclink.exe" if is_frozen() else "python -m pclink"


def app_dir() -> str:
    """Folder holding the executables (standalone) or the source checkout (Python)."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def service_binary() -> tuple[Optional[str], Optional[str]]:
    """``(exe, args)`` the service manager should run, or ``(None, None)`` to let
    pywin32 use its ``pythonservice.exe`` host (Python install)."""
    if is_frozen():
        return os.path.abspath(sys.executable), SERVICE_HOST_ARG
    return None, None
