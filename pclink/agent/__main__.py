"""``python -m pclink.agent``: how the service starts the agent from a Python install.

Uses an absolute import so the file also works as a plain script.
"""
import sys

from pclink.agent.main import main

if __name__ == "__main__":
    sys.exit(main())
