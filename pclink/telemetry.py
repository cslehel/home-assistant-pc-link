"""Cheap system metrics via psutil (CPU / memory / disks / network / battery).

``snapshot()`` is called on the service's main loop, so everything that can take
longer than a few milliseconds (the per-process scan for the "top processes",
the GPU counters, the free space of the disks) runs on its own thread and the snapshot only reads the last
result (``Background``, also used by ``devices.py``).
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import psutil

from .config import TelemetryConfig
from .util import slugify

log = logging.getLogger(__name__)

_GB = 1024 ** 3
_MB = 1024 ** 2

IDLE_PROCESS_NAMES = {"system idle process", "idle"}   # pid 0: its "CPU time" is the time nothing ran
_VIRTUAL_NIC_HINTS = ("vmware", "virtualbox", "vethernet", "hyper-v", "loopback", "bluetooth", "npcap", "tap-", "wsl",
                      "docker", "tailscale", "zerotier", "wireguard")


class Background:
    """The last result of a slow function, refreshed on a daemon thread.

    ``value()`` never blocks (apart from an optional short wait for the very
    first result).  The thread starts on first use and pauses while nobody has
    asked for the value for a while, so a disabled feature costs nothing.
    """

    def __init__(self, name: str, fn: Callable[[], Any], interval: Callable[[], float], initial: Any = None,
                 min_interval: float = 1.0):
        self.name = name
        self._fn = fn
        self._interval = interval
        self._min_interval = min_interval
        self._value = initial
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._have = threading.Event()
        self._asked = 0.0
        self.refreshed_at = 0.0
        self.last_seconds = 0.0
        self.errors = 0

    def _period(self) -> float:
        try:
            return max(self._min_interval, float(self._interval()))
        except Exception:  # noqa: BLE001
            return max(self._min_interval, 15.0)

    def value(self, wait_first: float = 0.0) -> Any:
        self._asked = time.monotonic()
        if self._thread is None:
            with self._lock:
                if self._thread is None and not self._stop.is_set():
                    self._thread = threading.Thread(target=self._loop, name=f"bg-{self.name}", daemon=True)
                    self._thread.start()
        if wait_first > 0 and not self._have.is_set():
            self._have.wait(wait_first)
        with self._lock:
            return self._value

    def refresh_now(self) -> None:
        """Run the function on the calling thread (the worker, and tests)."""
        started = time.monotonic()
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            if self.errors in (1, 10, 100):
                log.warning("%s refresh failed (%d): %s", self.name, self.errors, exc)
            return
        finally:
            self.last_seconds = time.monotonic() - started
        self.errors = 0
        with self._lock:
            self._value = result
            self.refreshed_at = time.monotonic()
        self._have.set()

    def request(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            period = self._period()
            # nobody reads the value any more (feature switched off, no client): do not work for nothing
            if time.monotonic() - self._asked <= max(60.0, period * 4):
                self.refresh_now()
            # a slow refresh must not turn into a busy thread
            self._wake.wait(max(period, self.last_seconds * 4))
            self._wake.clear()


def directed_broadcast(ipv4: Optional[str], netmask: Optional[str]) -> Optional[str]:
    """The subnet's broadcast address (where a Wake-on-LAN packet is sent); None when unknown."""
    if not ipv4 or not netmask:
        return None
    try:
        net = ipaddress.IPv4Network(f"{ipv4}/{netmask}", strict=False)
    except ValueError:
        return None
    if net.prefixlen >= 31:
        return None
    return str(net.broadcast_address)


def default_route_ip() -> Optional[str]:
    """The local address packets to the internet leave from (nothing is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # TEST-NET-1: only makes the system pick a route
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if not ip or ip.startswith(("127.", "0.")) else ip


def choose_primary(interfaces: list[dict[str, Any]], route_ip: Optional[str]) -> Optional[dict[str, Any]]:
    """Which interface is "the PC's" (pure; unit-tested).

    The one holding the default route.  Without a route (offline): a connected
    physical adapter before a virtual one (VMware, Hyper-V ... are always
    "up" with an address), then anything at all.
    """
    if route_ip:
        for i in interfaces:
            if i.get("ipv4") == route_ip:
                return i

    def rank(i: dict[str, Any]) -> tuple:
        name = str(i.get("name", "")).lower()
        virtual = any(h in name for h in _VIRTUAL_NIC_HINTS)
        linklocal = str(i.get("ipv4") or "").startswith("169.254.")
        return (not (i.get("up") and i.get("ipv4")), virtual, linklocal, name)

    ordered = sorted(interfaces, key=rank)
    return ordered[0] if ordered else None


def group_processes(rows: list[tuple[int, str, float, float]], ncpu: int, n: int) -> dict[str, Any]:
    """Top-N by CPU and by memory from (pid, name, cpu percent, rss bytes) rows (pure; unit-tested).

    Processes are grouped by image name.  Pid 0 is left out: the "System Idle
    Process" accumulates the time *nothing* ran, which made it the top process
    of every idle PC.
    """
    groups: dict[str, dict[str, Any]] = {}
    for pid, name, cpu, rss in rows:
        name = (name or "?").lower()
        if pid == 0 or name in IDLE_PROCESS_NAMES:
            continue
        g = groups.setdefault(name, {"name": name, "cpu": 0.0, "memory_mb": 0, "count": 0})
        g["cpu"] += (cpu or 0.0) / max(1, ncpu)
        g["memory_mb"] += (rss or 0) / _MB
        g["count"] += 1
    for g in groups.values():
        g["cpu"] = round(g["cpu"], 1)
        g["memory_mb"] = round(g["memory_mb"])
    by_cpu = sorted(groups.values(), key=lambda x: x["cpu"], reverse=True)[:n]
    by_mem = sorted(groups.values(), key=lambda x: x["memory_mb"], reverse=True)[:n]
    return {
        "top_processes": {"by_cpu": by_cpu, "cpu_count": ncpu},
        "top_process": by_cpu[0]["name"] if by_cpu else None,
        "top_process_cpu": by_cpu[0]["cpu"] if by_cpu else None,
        "top_memory_processes": {"by_memory": by_mem},
        "top_memory_process": by_mem[0]["name"] if by_mem else None,
        "top_memory_process_mb": by_mem[0]["memory_mb"] if by_mem else None,
    }


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
        # slow readers: each on its own thread, the snapshot takes the last result
        self._top = Background("top-processes", lambda: self.top_processes(self.cfg.top_processes),
                               lambda: self.cfg.interval_seconds, initial=group_processes([], 1, 0), min_interval=5.0)
        self._gpu = Background("gpu", lambda: self.gpu.read() if self.gpu is not None else None,
                               lambda: self.cfg.interval_seconds, min_interval=2.0)
        # free space of every fixed disk: a sleeping or network-backed volume can keep a call waiting
        self._disks = Background("disk-usage", self.disk_usage, lambda: self.cfg.interval_seconds,
                                 initial={}, min_interval=10.0)

    def stop(self) -> None:
        self._top.stop()
        self._gpu.stop()
        self._disks.stop()

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
        if self._interfaces_cache and now - self._interfaces_cache[0] < 60:
            return self._interfaces_cache[1]
        out: list[dict[str, Any]] = []
        try:
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                mac = ipv4 = netmask = None
                for a in addrs:
                    fam = getattr(a.family, "name", str(a.family))
                    if fam in ("AF_LINK", "AF_PACKET") or a.family == getattr(psutil, "AF_LINK", -1):
                        if a.address and (a.address.count("-") == 5 or a.address.count(":") == 5):
                            mac = a.address.replace("-", ":").upper()
                    elif a.family == socket.AF_INET and ipv4 is None:
                        ipv4, netmask = a.address, getattr(a, "netmask", None)
                if not mac or mac in ("00:00:00:00:00:00",):
                    continue
                st = stats.get(name)
                if ipv4 and ipv4.startswith("127."):
                    continue
                out.append({"name": name, "mac": mac, "ipv4": ipv4, "up": bool(st and st.isup),
                            "broadcast": directed_broadcast(ipv4, netmask), "primary": False})
        except Exception as exc:  # noqa: BLE001
            log.debug("net_if_addrs failed: %s", exc)
        # The PC's own interface is the one holding the default route; sorting by name put a
        # VMware / Hyper-V adapter in front of "Wi-Fi", and Wake-on-LAN went to the wrong MAC.
        primary = choose_primary(out, default_route_ip())
        if primary is not None:
            primary["primary"] = True
        out.sort(key=lambda i: (not i["primary"], not (i["up"] and i["ipv4"]), i["name"]))
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
            # read on its own thread (a short wait for the very first result, so the disks are
            # there from the first snapshot on)
            snap.update(self._disks.value(wait_first=1.0) or {})
        if self.cfg.disk_io:
            try:
                io = psutil.disk_io_counters()
                if io is not None:
                    if self._last_disk_io:
                        t0, last = self._last_disk_io
                        dt = max(now - t0, 0.001)
                        # totals drop when a disk / adapter disappears; a rate is never negative
                        snap["disk_read_mbps"] = round(max(0, io.read_bytes - last.read_bytes) / dt / _MB, 2)
                        snap["disk_write_mbps"] = round(max(0, io.write_bytes - last.write_bytes) / dt / _MB, 2)
                    self._last_disk_io = (now, io)
            except Exception as exc:  # noqa: BLE001
                log.debug("disk io failed: %s", exc)
        if self.cfg.network:
            try:
                net = psutil.net_io_counters()
                if self._last_net_io:
                    t0, last = self._last_net_io
                    dt = max(now - t0, 0.001)
                    snap["net_rx_mbps"] = round(max(0, net.bytes_recv - last.bytes_recv) * 8 / dt / 1e6, 3)
                    snap["net_tx_mbps"] = round(max(0, net.bytes_sent - last.bytes_sent) * 8 / dt / 1e6, 3)
                self._last_net_io = (now, net)
            except Exception as exc:  # noqa: BLE001
                log.debug("net io failed: %s", exc)
            ifaces = self.interfaces()
            snap["interfaces"] = ifaces
            primary = next((i for i in ifaces if i.get("primary")), ifaces[0] if ifaces else None)
            snap["mac"] = primary["mac"] if primary else None
            snap["ip"] = primary["ipv4"] if primary else None
            snap["broadcast"] = primary.get("broadcast") if primary else None   # where Wake-on-LAN is sent
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
            gpu = self._gpu.value()   # read on its own thread: counter queries can take a while
            if gpu:
                snap["gpu_name"] = gpu.get("name")
                snap["gpu_load"] = gpu.get("load")
                snap["gpu_memory_percent"] = gpu.get("memory_percent")
                snap["gpu_memory_used_mb"] = gpu.get("memory_used_mb")
                snap["gpu_memory_total_mb"] = gpu.get("memory_total_mb")
                snap["gpu_temperature"] = gpu.get("temperature")
                snap["gpu_power"] = gpu.get("power")
                if gpu.get("gpus"):
                    snap["gpus"] = {"gpus": gpu["gpus"]}
        if self.cfg.top_processes > 0:
            # the scan opens every process (about a second on a busy PC): never on the main loop
            snap.update(self._top.value())
        return snap

    def disk_usage(self) -> dict[str, Any]:
        """Size and free space of the fixed disks (on the ``disk-usage`` thread, see ``snapshot``)."""
        out: dict[str, Any] = {}
        disks = []
        for part in self._fixed_partitions():
            letter = self._letter(part)
            try:
                du = psutil.disk_usage(part.mountpoint)
            except Exception:  # noqa: BLE001
                continue
            disks.append(letter)
            key = slugify(letter)
            out[f"disk_{key}_percent"] = du.percent
            out[f"disk_{key}_free_gb"] = round(du.free / _GB, 1)
            out[f"disk_{key}_total_gb"] = round(du.total / _GB, 1)
        out["disks"] = disks
        return out

    def top_processes(self, n: int) -> dict[str, Any]:
        """Top-N processes by CPU and by memory, grouped by image name.

        psutil reports per-process CPU as "since the last call" for cached
        Process objects, so the first snapshot after start reads 0 % and every
        later one covers the telemetry interval - exactly what we want.
        Runs on the ``top-processes`` thread (see ``snapshot``).
        """
        ncpu = psutil.cpu_count() or 1
        rows: list[tuple[int, str, float, float]] = []
        for p in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
            try:
                info = p.info
                rows.append((p.pid, info["name"] or "?", info["cpu_percent"] or 0.0,
                             info["memory_info"].rss if info["memory_info"] else 0))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return group_processes(rows, ncpu, n)
