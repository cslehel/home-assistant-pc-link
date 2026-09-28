"""pywin32 Windows service wrapper.

Install / control with ``python -m pclink install|start|stop|remove``.
The service runs as LocalSystem so it can query user tokens (to start the
per-session agents), log sessions off and change account logon hours.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import traceback

from .config import Config, default_config_path
from .logsetup import setup_logging

log = logging.getLogger(__name__)

if sys.platform == "win32":
    import servicemanager  # type: ignore
    import win32event  # type: ignore
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore

    class PCLinkService(win32serviceutil.ServiceFramework):
        _svc_name_ = "PCLink"
        _svc_display_name_ = "Home Assistant PC Link"
        _svc_description_ = "Screen-time limits, login blocking and PC telemetry for Home Assistant (MQTT)."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.orchestrator = None
            socket.setdefaulttimeout(60)

        def GetAcceptedControls(self):
            accepted = super().GetAcceptedControls()
            accepted |= win32service.SERVICE_ACCEPT_SESSIONCHANGE
            return accepted

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.orchestrator is not None:
                self.orchestrator.stop()
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self):
            self.SvcStop()

        def SvcOtherEx(self, control, event_type, data):
            if control == win32service.SERVICE_CONTROL_SESSIONCHANGE and self.orchestrator is not None:
                session_id = data[-1] if isinstance(data, (tuple, list)) else data
                try:
                    self.orchestrator.on_session_change(int(event_type), int(session_id))
                except Exception:  # noqa: BLE001
                    log.exception("session change handling failed")

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                                  (self._svc_name_, ""))
            try:
                self._run()
            except Exception:  # noqa: BLE001
                servicemanager.LogErrorMsg(f"PCLink failed:\n{traceback.format_exc()}")
                log.exception("service crashed")
                raise
            finally:
                servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED,
                                      (self._svc_name_, ""))

        def _run(self):
            config_path = default_config_path()
            fallback_logs = os.path.join(os.path.dirname(config_path), "logs")
            setup_logging(fallback_logs, "INFO")
            log.info("loading config from %s", config_path)
            cfg = Config.load(config_path)
            setup_logging(cfg.logging.path, cfg.logging.level, max_bytes=cfg.logging.max_bytes, backups=cfg.logging.backups)
            from .core import build_orchestrator

            self.orchestrator = build_orchestrator(cfg)
            self.orchestrator.run()

    def handle_command_line(argv: list[str]) -> None:
        from . import hardening

        name = PCLinkService._svc_name_
        # Custom verbs that HandleCommandLine does not know about.
        if argv and argv[0] == "harden":
            hardening.apply_all(name, harden=True)
            print("service hardened: auto-restart on; only administrators can stop it.")
            print("current descriptor:", hardening.show_acl(name))
            return
        if argv and argv[0] == "unharden":
            hardening.soften_acl(name)
            print("service ACL restored to default; standard controls apply again.")
            return

        no_harden = "--no-harden" in argv
        argv = [a for a in argv if a != "--no-harden"]
        if "install" in argv and "--startup" not in argv:
            argv = ["--startup", "auto"] + argv

        # Before removing: stop the service, restore logon rights for every managed
        # user (so nobody stays locked out once enforcement is gone) and restore the
        # default ACL so the SCM can delete the service.
        if "remove" in argv:
            try:
                win32serviceutil.StopService(name)
            except Exception:  # noqa: BLE001
                pass
            try:
                from .config import Config
                from .logonblock import make_blocker

                cfg = Config.load(default_config_path())
                blocker = make_blocker(cfg.time_limits.block_method)
                for policy in cfg.time_limits.users:
                    if not policy.unlimited:
                        blocker.unblock(policy.name)
                print("restored logon rights for managed users")
            except Exception as exc:  # noqa: BLE001
                print(f"note: could not restore logon rights automatically ({exc}); run 'python -m pclink unblock-all'")
            try:
                hardening.soften_acl(name)
            except Exception:  # noqa: BLE001
                log.debug("soften before remove failed", exc_info=True)

        win32serviceutil.HandleCommandLine(PCLinkService, argv=[sys.argv[0], *argv])

        # After install, set recovery actions and (unless opted out) harden the ACL.
        if "install" in argv:
            try:
                hardening.set_recovery(name)
                if not no_harden:
                    hardening.harden_acl(name)
                    print("Tamper resistance enabled: the service auto-restarts and only")
                    print("administrators can stop it. Keep monitored users as STANDARD accounts.")
                    print("Run 'python -m pclink unharden' before uninstalling, or use 'remove'")
                    print("which restores the default ACL automatically.")
            except Exception:  # noqa: BLE001
                log.exception("post-install hardening failed")

else:  # pragma: no cover - non-Windows import guard

    def handle_command_line(argv: list[str]) -> None:  # type: ignore[misc]
        raise SystemExit("Windows service commands are only available on Windows")
