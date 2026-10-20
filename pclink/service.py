"""pywin32 Windows service wrapper.

Install / control with ``pclink.exe install|start|stop|remove`` (standalone
build) or ``python -m pclink ...`` (Python install).  The service runs as
LocalSystem so it can query user tokens (to start the per-session agents), log
sessions off and change account logon hours.

Standalone build: the service manager runs ``pclink.exe service``, which hosts
the service in-process through ``servicemanager``.  Python install: pywin32's
``pythonservice.exe`` hosts the class as usual.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import traceback

from .config import Config, data_dir, default_config_path
from .logsetup import setup_logging
from .runtime import cli_name, service_binary

log = logging.getLogger(__name__)


def event_log(write, *args) -> None:
    """Write to Windows' Application log, never failing the service over it: right after the
    service was registered Windows has been seen to refuse the first message (ERROR_ACCESS_DENIED
    from ReportEvent), and that must not stop the limits from being enforced."""
    try:
        write(*args)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write to the Windows event log: %s", exc)


if sys.platform == "win32":
    import servicemanager  # type: ignore
    import win32event  # type: ignore
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore

    class PCLinkService(win32serviceutil.ServiceFramework):
        _svc_name_ = "PCLink"
        _svc_display_name_ = "Home Assistant PC Link"
        _svc_description_ = "Screen-time limits, login blocking and PC telemetry for Home Assistant."

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
            event_log(servicemanager.LogMsg, servicemanager.EVENTLOG_INFORMATION_TYPE,
                      servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
            try:
                self._run()
            except Exception:  # noqa: BLE001
                event_log(servicemanager.LogErrorMsg, f"PCLink failed:\n{traceback.format_exc()}")
                log.exception("service crashed")
                raise
            finally:
                event_log(servicemanager.LogMsg, servicemanager.EVENTLOG_INFORMATION_TYPE,
                          servicemanager.PYS_SERVICE_STOPPED, (self._svc_name_, ""))

        def _run(self):
            from . import hardening

            config_path = default_config_path()
            # Before anything is written: a data folder that is a link leads wherever somebody
            # wanted it to.  Not even the log goes there.
            problem = hardening.data_dir_problem(data_dir())
            if problem:
                raise hardening.DataDirError(problem)
            fallback_logs = os.path.join(os.path.dirname(config_path), "logs")
            setup_logging(fallback_logs, "INFO")
            # The folder holds the configuration, and the configuration says what runs as
            # SYSTEM.  If it cannot be restricted to administrators the service does not
            # start (hardening.ensure_data_dir says why that is the lesser evil); Windows
            # starts it again after a few seconds, so a passing problem heals by itself.
            try:
                hardening.ensure_data_dir(data_dir())
            except hardening.DataDirError as exc:
                log.error("NOT starting, the data folder is not safe to use: %s", exc)
                raise
            log.info("loading config from %s", config_path)
            cfg, config_problem = hardening.load_config_or_last_good(config_path, data_dir())
            setup_logging(cfg.logging.path, cfg.logging.level, max_bytes=cfg.logging.max_bytes, backups=cfg.logging.backups)
            if config_problem:
                log.error("CONFIGURATION ERROR: %s. Correct the file; it is read again as soon as it changes.",
                          config_problem)
                servicemanager.LogErrorMsg(f"PCLink: {config_problem}")
            from .core import build_orchestrator

            self.orchestrator = build_orchestrator(cfg)
            if config_problem:
                self.orchestrator.config_problem = config_problem   # for whoever reports the service's health
                try:
                    self.orchestrator.store.log_event(None, "config_invalid", config_problem[:300])
                except Exception:  # noqa: BLE001
                    log.debug("could not store the configuration error as an event", exc_info=True)
            self.orchestrator.run()

    # Standalone build: register "<path>\\pclink.exe service" as the service binary.
    _exe_name, _exe_args = service_binary()
    if _exe_name:
        PCLinkService._exe_name_ = _exe_name
        PCLinkService._exe_args_ = _exe_args

    def run_service_host() -> int:
        """Entry point when the service manager starts the standalone build."""
        try:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(PCLinkService)
            servicemanager.StartServiceCtrlDispatcher()
        except Exception as exc:  # noqa: BLE001 - e.g. error 1063 when started from a console
            print(f"'{cli_name()} service' is started by Windows' service manager, not by hand ({exc}).\n"
                  f"Use '{cli_name()} start' to start the installed service, or '{cli_name()} run' to run in this console.")
            return 1
        return 0

    def handle_command_line(argv: list[str]) -> None:
        from . import hardening

        name = PCLinkService._svc_name_
        # Custom verbs that HandleCommandLine does not know about.
        if argv and argv[0] == "harden":
            done = hardening.apply_all(name, harden=True)
            done["data folder restricted to administrators"] = _secure_data_dir(hardening)
            for what, ok in done.items():
                print(f"  {what}: {'ok' if ok else 'FAILED'}")
            if not all(done.values()):
                raise SystemExit("Hardening did NOT complete (see above). Run this from an administrator prompt, "
                                 f"with the {name} service installed.")
            print("service hardened: auto-restart on; only administrators can stop it.")
            print("current descriptor:", hardening.show_acl(name))
            return
        if argv and argv[0] == "unharden":
            if not hardening.soften_acl(name):
                raise SystemExit("The service ACL could NOT be restored. Run this from an administrator prompt, "
                                 f"with the {name} service installed.")
            print("service ACL restored to default; standard controls apply again.")
            return

        no_harden = "--no-harden" in argv
        argv = [a for a in argv if a != "--no-harden"]
        if "install" in argv and "--startup" not in argv:
            argv = ["--startup", "auto"] + argv
        if "install" in argv or "update" in argv:
            # never register a service whose data folder somebody else controls
            problem = hardening.data_dir_problem(data_dir())
            if problem:
                raise SystemExit(f"Not installing: {problem}.")

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
                print(f"note: could not restore logon rights automatically ({exc}); run '{cli_name()} unblock-all'")
            try:
                hardening.soften_acl(name)
            except Exception:  # noqa: BLE001
                log.debug("soften before remove failed", exc_info=True)

        try:
            win32serviceutil.HandleCommandLine(PCLinkService, argv=[sys.argv[0], *argv])
        except Exception as exc:  # noqa: BLE001 - pywintypes.error: say what to do instead of a traceback
            code = getattr(exc, "winerror", None)
            if code == 5:  # ERROR_ACCESS_DENIED
                raise SystemExit("Access is denied: service commands need an administrator prompt "
                                 "(right-click PowerShell or Command Prompt and choose 'Run as administrator').") from None
            if code == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
                raise SystemExit(f"The {name} service is not installed; run '{cli_name()} install' first.") from None
            raise

        # After install, set recovery actions and (unless opted out) harden the ACL.
        if "install" in argv:
            failed = []
            if not hardening.set_recovery(name):
                failed.append("auto-restart")
            if not _secure_data_dir(hardening):
                failed.append("restricting the data folder to administrators")
            if not no_harden and not hardening.harden_acl(name):
                failed.append("service permissions")
            if failed:
                raise SystemExit("The service was installed, but this did NOT work: " + ", ".join(failed)
                                 + f". Run '{cli_name()} harden' from an administrator prompt before starting it.")
            if not no_harden:
                print("Tamper resistance enabled: the service auto-restarts and only")
                print("administrators can stop it. Keep monitored users as STANDARD accounts.")
                print(f"Run '{cli_name()} unharden' before uninstalling, or use 'remove'")
                print("which restores the default ACL automatically.")

    def _secure_data_dir(hardening) -> bool:
        try:
            hardening.ensure_data_dir(data_dir())
            return True
        except hardening.DataDirError as exc:
            print(f"  data folder: {exc}")
            return False

else:  # pragma: no cover - non-Windows import guard

    def handle_command_line(argv: list[str]) -> None:  # type: ignore[misc]
        raise SystemExit("Windows service commands are only available on Windows")

    def run_service_host() -> int:  # type: ignore[misc]
        raise SystemExit("the Windows service host is only available on Windows")
