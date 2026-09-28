"""PyInstaller entry script for pclink.exe (command line + Windows service host).

Entry scripts run as top-level ``__main__``, so they must use absolute imports;
``pclink/__main__.py`` itself uses relative ones and cannot be the entry script.
"""
import sys

from pclink.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
