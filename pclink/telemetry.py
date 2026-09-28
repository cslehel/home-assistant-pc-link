"""Cheap system metrics via psutil (CPU / memory / disks / network / battery)."""
from __future__ import annotations

import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional

import psutil

from .config import TelemetryConfig
from .util import slugify

log = logging.getLogger(__name__)

_GB = 1024 ** 3
_MB = 1024 ** 2


class Telemetry:
    def __init__(self, cfg: TelemetryConfig, gpu=None):
        self.cfg = cfg
        self.gpu = gpu
        self._last_disk_io: Optional[tuple[float, Any]] = None
        self._last_net_io: Optional[tuple[float, Any]] = None
        self._disk_letters: list[str] = []
        self._interfaces_cache: Optional[tuple[float, list[dict[str, Any]]]] = None
        psutil.cpu_percent(interval=None)  # prime the non-blocking counter
        self._partitions_checked = 0.0
        self._partitions: list[Any] = []

    # -- helpers --------------------------------------------------------------
    def _fixed_partitions(self) -> list[Any]:
        now = time.monotonic()
        if now - self._partitions_checked > 300 or not self._partitions:
            parts = []
            try:
                for p in psutil.disk_partitions(all=False):
                    opts = (p.opts or "").lower()
                    if "cdrom" in opts or not p.fstype or "removable" in opts:
                        continue
                    parts.append(p)
            except Exception as exc:  # noqa: BLE001
                log.debug("disk_partitions failed: %s", exc)
            self._partitions = parts
            self._partitions_checked = now
            self._disk_letters = [self._letter(p) for p in parts]
        return self._partitions

    @staticmethod
    def _letter(part) -> str:
        mp = part.mountpoint or part.device or "?"
        if len(mp) >= 2 and mp[1] == ":":
            return mp[0].lower()
        return slugify(mp) or "root"

    @property
    def disk_letters(self) -> list[str]:
        self._fixed_partitions()
        return list(self._disk_letters)

    def interfaces(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._interfaces_cache and now - self._interfaces_cache[0] < 300:
            return self._interfaces_cache[1]
        out: list[dict[str, Any]] = []
        try:
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                mac = ipv4 = None
                for a in addrs:
                    fam = getattr(a.family, "name", str(a.family))
                    if fam in ("AF_LINK", "AF_PACKET") or a.family == getattr(psutil, "AF_LINK", -1):
                        if a.address and a.address.count("-") == 5 or a.address.count(":") == 5:
                            mac = a.address.replace("-", ":").upper()
                    elif a.family == socket.AF_INET:
                        ipv4 = a.address
                if not mac or mac in ("00:00:00:00:00:00",):
                    continue
                st = stats.get(name)
                if ipv4 and ipv4.startswith("127."):
                    continue
                out.append({"name": name, "mac": mac, "ipv4": ipv4, "up": bool(st and st.isup)})
        except Exception as exc:  # noqa: BLE001
            log.debug("net_if_addrs failed: %s", exc)
        out.sort(key=lambda i: (not (i["up"] and i["ipv4"]), i["name"]))
        self._interfaces_cache = (now, out)
        return out

    # -- snapshot ---------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        snap: dict[str, Any] = {}
        try:
            snap["cpu_percent"] = psutil.cpu_percent(interval=None)
            freq = psutil.cpu_freq()
            snap["cpu_freq_mhz"] = round(freq.current) if freq else None
            snap["cpu_count"] = psutil.cpu_count()
        except Exception as exc:  # noqa: BLE001
            log.debug("cpu metrics failed: %s", exc)
        try:
            vm = psutil.virtual_memory()
            snap["memory_percent"] = vm.percent
            snap["memory_used_gb"] = round((vm.total - vm.available) / _GB, 2)
            snap["memory_total_gb"] = round(vm.total / _GB, 2)
            sw = psutil.swap_memory()
            snap["swap_percent"] = sw.percent
        except Exception as exc:  # noqa: BLE001
            log.debug("memory metrics failed: %s", exc)
        if self.cfg.disks:
            disks = []
            for part in self._fixed_partitions():
                letter = self._letter(part)
                try:
                    du = psutil.disk_usage(part.mountpoint)
                except Exception:  # noqa: BLE001
                    continue
                disks.append(letter)
                key = slugify(letter)
                snap[f"disk_{key}_percent"] = du.percent
                snap[f"disk_{key}_free_gb"] = round(du.free / _GB, 1)
                snap[f"disk_{key}_total_gb"] = round(du.total / _GB, 1)
            snap["disks"] = disks
        if self.cfg.disk_io:
            try:
                io = psutil.disk_io_counters()
                if io is not None:
                    if self._last_disk_io:
                        t0, last = self._last_disk_io
                        dt = max(now - t0, 0.001)
                        snap["disk_read_mbps"] = round((io.read_bytes - last.read_bytes) / dt / _MB, 2)
                        snap["disk_write_mbps"] = round((io.write_bytes - last.write_bytes) / dt / _MB, 2)
                    self._last_disk_io = (now, io)
            except Exception as exc:  # noqa: BLE001
                log.debug("disk io failed: %s", exc)
        if self.cfg.network:
            try:
                net = psutil.net_io_counters()
                if self._last_net_io:
                    t0, last = self._last_net_io
                    dt = max(now - t0, 0.001)
                    snap["net_rx_mbps"] = round((net.bytes_recv - last.bytes_recv) * 8 / dt / 1e6, 3)
                    snap["net_tx_mbps"] = round((net.bytes_sent - last.bytes_sent) * 8 / dt / 1e6, 3)
                self._last_net_io = (now, net)
            except Exception as exc:  # noqa: BLE001
                log.debug("net io failed: %s", exc)
            ifaces = self.interfaces()
            snap["interfaces"] = ifaces
            primary = next((i for i in ifaces if i["up"] and i["ipv4"]), ifaces[0] if ifaces else None)
            snap["mac"] = primary["mac"] if primary else None
            snap["ip"] = primary["ipv4"] if primary else None
        try:
            boot = psutil.boot_time()
            snap["last_boot"] = datetime.fromtimestamp(boot, tz=timezone.utc).isoformat()
            snap["uptime_hours"] = round((time.time() - boot) / 3600, 2)
        except Exception as exc:  # noqa: BLE001
            log.debug("boot_time failed: %s", exc)
        try:
            snap["process_count"] = len(psutil.pids())
        except Exception:  # noqa: BLE001
            pass
        if self.cfg.battery:
            try:
                bat = psutil.sensors_battery()
                if bat is not None:
                    snap["battery_percent"] = round(bat.percent)
                    snap["battery_plugged"] = bool(bat.power_plugged)
            except Exception:  # noqa: BLE001
                pass
        if self.cfg.gpu and self.gpu is not None:
            try:
                gpu = self.gpu.read()
            except Exception as exc:  # noqa: BLE001
                log.debug("gpu read failed: %s", exc)
                gpu = None
            if gpu:
                snap["gpu_name"] = gpu.get("name")
                snap["gpu_load"] = gpu.get("load")
                snap["gpu_memory_percent"] = gpu.get("memory_percent")
                snap["gpu_memory_used_mb"] = gpu.get("memory_used_mb")
                snap["gpu_memory_total_mb"] = gpu.get("memory_total_mb")
                snap["gpu_temperature"] = gpu.get("temperature")
                snap["gpu_power"] = gpu.get("power")
        if self.cfg.top_processes > 0:
            snap["top_processes"] = self._top_processes(self.cfg.top_processes)
        return snap

    def _top_processes(self, n: int) -> list[dict[str, Any]]:
        procs = []
        for p in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                procs.append({"name": info["name"], "cpu": info["cpu_percent"] or 0.0,
                              "memory_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / _MB)})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x["cpu"], reverse=True)
        return procs[:n]
