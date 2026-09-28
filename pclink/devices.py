"""Connected USB devices, removable drives and printer status.

USB devices are enumerated with SetupAPI through ctypes (no subprocess, no
WMI, a few milliseconds), printers through ``win32print``.  Both are polled
on their own slow intervals and cached; both return empty results off Windows.
"""
from __future__ import annotations

import ctypes
import logging
import re
import sys
import time
from typing import Any, Callable, Optional

from .config import TelemetryConfig

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"
_VID_PID_RE = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)

# SetupAPI constants
_DIGCF_PRESENT = 0x02
_DIGCF_ALLCLASSES = 0x04
_SPDRP_DEVICEDESC = 0x00
_SPDRP_HARDWAREID = 0x01
_SPDRP_CLASS = 0x07
_SPDRP_FRIENDLYNAME = 0x0C
_INVALID_HANDLE = ctypes.c_void_p(-1).value

# printer status bits (winspool)
_PRINTER_STATUS = [
    (0x00000001, "paused"), (0x00000002, "error"), (0x00000004, "pending deletion"), (0x00000008, "paper jam"),
    (0x00000010, "paper out"), (0x00000020, "manual feed"), (0x00000040, "paper problem"), (0x00000080, "offline"),
    (0x00000100, "io active"), (0x00000200, "busy"), (0x00000400, "printing"), (0x00000800, "output bin full"),
    (0x00001000, "not available"), (0x00002000, "waiting"), (0x00004000, "processing"), (0x00008000, "initializing"),
    (0x00010000, "warming up"), (0x00020000, "toner low"), (0x00040000, "no toner"), (0x00080000, "page punt"),
    (0x00100000, "user intervention"), (0x00200000, "out of memory"), (0x00400000, "door open"),
    (0x00800000, "server unknown"), (0x01000000, "power save"),
]
_PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x400
_PRINTER_ATTRIBUTE_DEFAULT = 0x4
_ERROR_BITS = 0x00000002 | 0x8 | 0x10 | 0x40 | 0x1000 | 0x40000 | 0x100000 | 0x400000
_BUSY_BITS = 0x100 | 0x200 | 0x400 | 0x4000 | 0x8000 | 0x10000


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("ClassGuid", ctypes.c_byte * 16),
                ("DevInst", ctypes.c_uint32), ("Reserved", ctypes.c_void_p)]


def usb_devices() -> list[dict[str, Any]]:
    """Present devices on the USB bus: [{name, class, instance_id, vid, pid, hub}]."""
    if not _IS_WIN:
        return []
    try:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    except OSError:
        return []
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ]
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

    hdev = setupapi.SetupDiGetClassDevsW(None, "USB", None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES)
    if not hdev or hdev == _INVALID_HANDLE:
        return []
    out: list[dict[str, Any]] = []
    try:
        index = 0
        info = _SP_DEVINFO_DATA()
        info.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        buf = ctypes.create_unicode_buffer(1024)
        while setupapi.SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(info)):
            index += 1

            def prop(which: int) -> str:
                needed = ctypes.c_uint32(0)
                ok = setupapi.SetupDiGetDeviceRegistryPropertyW(hdev, ctypes.byref(info), which, None, buf,
                                                                ctypes.sizeof(buf), ctypes.byref(needed))
                if not ok:
                    return ""
                if which == _SPDRP_HARDWAREID:  # REG_MULTI_SZ: take the first entry
                    return buf.value
                return buf.value

            name = prop(_SPDRP_FRIENDLYNAME) or prop(_SPDRP_DEVICEDESC)
            dev_class = prop(_SPDRP_CLASS)
            hwid = prop(_SPDRP_HARDWAREID)
            needed = ctypes.c_uint32(0)
            inst = ""
            if setupapi.SetupDiGetDeviceInstanceIdW(hdev, ctypes.byref(info), buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                inst = buf.value
            m = _VID_PID_RE.search(hwid or inst)
            lname = (name or "").lower()
            hub = "hub" in lname or "root" in lname or dev_class.lower() == "usb" and "composite" in lname
            out.append({
                "name": name or inst or "USB device", "class": dev_class, "instance_id": inst,
                "vid": m.group(1).upper() if m else None, "pid": m.group(2).upper() if m else None, "hub": bool(hub),
            })
    except Exception as exc:  # noqa: BLE001
        log.debug("USB enumeration failed: %s", exc)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    out.sort(key=lambda d: (d["hub"], d["class"], d["name"]))
    return out


def removable_drives() -> list[dict[str, Any]]:
    try:
        import psutil
    except ImportError:
        return []
    out = []
    try:
        for p in psutil.disk_partitions(all=True):
            opts = (p.opts or "").lower()
            if "removable" not in opts and "cdrom" not in opts:
                continue
            entry: dict[str, Any] = {"mount": p.mountpoint, "fstype": p.fstype or None, "kind": "cdrom" if "cdrom" in opts else "removable"}
            try:
                du = psutil.disk_usage(p.mountpoint)
                entry["total_gb"] = round(du.total / 1024 ** 3, 1)
                entry["free_gb"] = round(du.free / 1024 ** 3, 1)
            except Exception:  # noqa: BLE001
                pass
            out.append(entry)
    except Exception as exc:  # noqa: BLE001
        log.debug("disk_partitions failed: %s", exc)
    return out


def _status_words(status: int, attributes: int) -> list[str]:
    words = [label for bit, label in _PRINTER_STATUS if status & bit]
    if attributes & _PRINTER_ATTRIBUTE_WORK_OFFLINE and "offline" not in words:
        words.append("offline")
    return words


def printers() -> dict[str, Any]:
    """{"printers": {name: {...}}, "printer_status": summary, "print_jobs": n}"""
    if not _IS_WIN:
        return {"printers": {}, "printer_status": "unknown", "print_jobs": 0}
    try:
        import win32print  # type: ignore
    except ImportError:
        return {"printers": {}, "printer_status": "unknown", "print_jobs": 0}
    result: dict[str, Any] = {}
    jobs_total = 0
    worst = "idle"
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for entry in win32print.EnumPrinters(flags, None, 2):
            name = entry.get("pPrinterName") or "?"
            status = int(entry.get("Status") or 0)
            attributes = int(entry.get("Attributes") or 0)
            jobs = int(entry.get("cJobs") or 0)
            words = _status_words(status, attributes)
            if status & _ERROR_BITS:
                state = "error"
            elif "offline" in words:
                state = "offline"
            elif status & _BUSY_BITS or jobs:
                state = "printing"
            elif status & 0x1:
                state = "paused"
            else:
                state = "idle"
            result[name] = {
                "state": state, "status": ", ".join(words) or "ready", "jobs": jobs,
                "default": bool(attributes & _PRINTER_ATTRIBUTE_DEFAULT), "driver": entry.get("pDriverName"),
                "port": entry.get("pPortName"),
            }
            jobs_total += jobs
            rank = {"idle": 0, "paused": 1, "printing": 2, "offline": 3, "error": 4}
            if rank[state] > rank[worst]:
                worst = state
    except Exception as exc:  # noqa: BLE001
        log.debug("EnumPrinters failed: %s", exc)
        return {"printers": {}, "printer_status": "unknown", "print_jobs": 0}
    return {"printers": result, "printer_status": worst if result else "none", "print_jobs": jobs_total}


class DeviceMonitor:
    """Cached USB / drive / printer snapshots on slow intervals."""

    def __init__(self, cfg: TelemetryConfig, on_event: Optional[Callable[[str, dict[str, Any]], None]] = None):
        self.cfg = cfg
        self._on_event = on_event
        self._usb: list[dict[str, Any]] = []
        self._drives: list[dict[str, Any]] = []
        self._usb_at = 0.0
        self._printers: dict[str, Any] = {}
        self._printers_at = 0.0
        self._usb_seen = False

    def update_config(self, cfg: TelemetryConfig) -> None:
        self.cfg = cfg

    def snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {}
        now = time.monotonic()
        if self.cfg.usb_devices:
            if now - self._usb_at >= self.cfg.devices_interval_seconds or not self._usb_seen:
                new = usb_devices()
                drives = removable_drives()
                if self._usb_seen and {d["instance_id"] for d in new} != {d["instance_id"] for d in self._usb}:
                    self._emit("usb_changed", {"count": len(new)})
                self._usb, self._drives, self._usb_at, self._usb_seen = new, drives, now, True
            snap["usb_devices"] = len([d for d in self._usb if not d["hub"]])
            snap["usb_device_list"] = {"devices": self._usb, "hubs": len([d for d in self._usb if d["hub"]])}
            snap["removable_drives"] = len(self._drives)
            snap["removable_drive_list"] = {"drives": self._drives}
        if self.cfg.printers:
            if now - self._printers_at >= self.cfg.printers_interval_seconds or not self._printers:
                new = printers()
                old_state = self._printers.get("printer_status")
                if old_state is not None and new.get("printer_status") == "error" and old_state != "error":
                    self._emit("printer_error", {"printers": new.get("printers")})
                self._printers, self._printers_at = new, now
            snap.update(self._printers)
        return snap

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(kind, data)
            except Exception:  # noqa: BLE001
                log.debug("device event handler failed", exc_info=True)
