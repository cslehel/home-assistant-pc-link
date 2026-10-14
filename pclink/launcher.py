"""Start the per-session user agent inside a user's desktop session.

The service runs as LocalSystem in session 0 and cannot see the foreground
window, the URL bar or user idle time of a desktop session.  For each active
session (each ASTER seat) it launches the agent with the session user's token,
so the agent runs as that user on that desktop: ``pclink-agent.exe`` in the
standalone build, ``pythonw.exe -m pclink.agent`` from a Python install.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional

from .config import AgentConfig
from .ipc import remember_launch
from .runtime import AGENT_EXE, is_frozen

log = logging.getLogger(__name__)

STILL_ACTIVE = 259


@dataclass
class AgentHandle:
    session_id: int
    pid: int
    process: Any
    token: str
    started_at: float


def find_pythonw(explicit: Optional[str] = None) -> str:
    if explicit and os.path.isfile(explicit):
        return explicit
    candidates = []
    for prefix in (sys.prefix, sys.exec_prefix, sys.base_prefix, getattr(sys, "base_exec_prefix", sys.base_prefix)):
        candidates.append(os.path.join(prefix, "pythonw.exe"))
        candidates.append(os.path.join(prefix, "Scripts", "pythonw.exe"))
        candidates.append(os.path.join(prefix, "python.exe"))
    exe_dir = os.path.dirname(sys.executable)
    candidates.append(os.path.join(exe_dir, "pythonw.exe"))
    candidates.append(os.path.join(exe_dir, "python.exe"))
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return sys.executable


def agent_command(python_path: Optional[str] = None, *, frozen: Optional[bool] = None,
                  executable: Optional[str] = None) -> tuple[str, str, bool]:
    """Return ``(command line, working directory, needs PYTHONPATH)`` for starting an agent.

    Standalone build: ``pclink-agent.exe`` next to ``pclink.exe`` (windowed, so no
    console flashes up), falling back to ``pclink.exe agent``.  Python install:
    ``pythonw.exe -m pclink.agent`` with the source folder on PYTHONPATH.
    """
    frozen = is_frozen() if frozen is None else frozen
    executable = os.path.abspath(executable or sys.executable)
    if frozen:
        base = os.path.dirname(executable)
        agent = os.path.join(base, AGENT_EXE)
        if os.path.isfile(agent):
            return f'"{agent}"', base, False
        return f'"{executable}" agent', base, False
    python = find_pythonw(python_path)
    return f'"{python}" -X utf8 -m pclink.agent', os.path.dirname(python), True


def package_root() -> str:
    import pclink

    return os.path.dirname(os.path.dirname(os.path.abspath(pclink.__file__)))


class AgentLauncher:
    def __init__(self, cfg: AgentConfig, agent_settings: dict[str, Any]):
        if sys.platform != "win32":
            raise RuntimeError("AgentLauncher only works on Windows")
        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32process  # type: ignore
        import win32profile  # type: ignore
        import win32ts  # type: ignore

        self._win32api, self._win32con, self._win32process, self._win32profile, self._win32ts = (
            win32api, win32con, win32process, win32profile, win32ts
        )
        self.cfg = cfg
        self.agent_settings = agent_settings
        self.command, self.cwd, self.needs_pythonpath = agent_command(cfg.python_path)
        log.info("agent command: %s", self.command)

    def launch(self, session_id: int, token: str, user: str) -> Optional[AgentHandle]:
        import time

        ts, prof, proc, con = self._win32ts, self._win32profile, self._win32process, self._win32con
        try:
            user_token = ts.WTSQueryUserToken(session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("WTSQueryUserToken(%s) failed: %s", session_id, exc)
            return None
        try:
            try:
                env = prof.CreateEnvironmentBlock(user_token, False)
            except Exception as exc:  # noqa: BLE001
                log.debug("CreateEnvironmentBlock failed: %s", exc)
                env = dict(os.environ)
            env = {str(k): str(v) for k, v in env.items()}
            if self.needs_pythonpath:
                existing = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = package_root() + (os.pathsep + existing if existing else "")
            env["PCLINK_SESSION"] = str(session_id)
            env["PCLINK_USER"] = user
            env["PCLINK_AGENT_TOKEN"] = token
            env["PCLINK_IPC_HOST"] = self.cfg.ipc_host
            env["PCLINK_IPC_PORT"] = str(self.cfg.ipc_port)
            env["PCLINK_AGENT_CONFIG"] = json.dumps(self.agent_settings)
            env["PCLINK_SERVICE_PID"] = str(os.getpid())   # the agent never closes this process, whatever it is asked
            env["PYTHONIOENCODING"] = "utf-8"
            si = proc.STARTUPINFO()
            si.lpDesktop = "winsta0\\default"
            si.dwFlags = con.STARTF_USESHOWWINDOW
            si.wShowWindow = con.SW_HIDE
            remember_launch(session_id, token)   # the agent server answers this agent's challenge with it
            flags = con.CREATE_UNICODE_ENVIRONMENT | con.CREATE_NEW_PROCESS_GROUP | getattr(con, "CREATE_NO_WINDOW", 0x08000000)
            handle, thread, pid, _tid = proc.CreateProcessAsUser(user_token, None, self.command, None, None, False,
                                                                 flags, env, self.cwd, si)
            thread.Close()
            log.info("started agent for session %s (user %s, pid %s)", session_id, user, pid)
            return AgentHandle(session_id=session_id, pid=pid, process=handle, token=token, started_at=time.time())
        except Exception as exc:  # noqa: BLE001
            log.error("could not start agent in session %s: %s", session_id, exc)
            return None
        finally:
            try:
                user_token.Close()
            except Exception:  # noqa: BLE001
                pass

    def alive(self, handle: AgentHandle) -> bool:
        try:
            return self._win32process.GetExitCodeProcess(handle.process) == STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return False

    def terminate(self, handle: AgentHandle) -> None:
        try:
            if self.alive(handle):
                self._win32api.TerminateProcess(handle.process, 0)
        except Exception as exc:  # noqa: BLE001
            log.debug("terminate agent %s failed: %s", handle.pid, exc)
        self.release(handle)

    def release(self, handle: AgentHandle) -> None:
        try:
            handle.process.Close()
        except Exception:  # noqa: BLE001
            pass
