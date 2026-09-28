"""PyInstaller entry script for pclink-agent.exe (per-session agent, windowed)."""
import sys

from pclink.agent.main import main

if __name__ == "__main__":
    sys.exit(main())
