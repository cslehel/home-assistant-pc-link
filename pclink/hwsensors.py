"""Hardware sensor readings (CPU / GPU / motherboard temperatures, fans, power).

Backends:

* ``lhm``      - LibreHardwareMonitorLib.dll loaded in-process through pythonnet.
                 Runs inside the SYSTEM service, so it can access the kernel
                 driver.  Best option; no GUI app needed.
* ``lhm_http`` - The LibreHardwareMonitor desktop app's "Remote web server"
                 JSON endpoint (data.json).  Useful when the app already runs.
* ``none``

Storage (SMART) is polled less often than the rest because it is slow.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from typing import Any, Optional

from .config import HardwareSensorsConfig
from .models import HwReading
from .util import slugify

log = logging.getLogger(__name__)

_UNIT_BY_TYPE = {
    "temperature": "°C", "load": "%", "fan": "RPM", "power": "W", "clock": "MHz", "voltage": "V",
    "current": "A", "data": "GB", "smalldata": "MB", "throughput": "B/s", "control": "%", "level": "%",
    "factor": "", "frequency": "Hz", "energy": "mWh", "noise": "dBA", "humidity": "%", "timespan": "s",
    "flow": "L/h", "conductivity": "µS/cm",
}


# Captions of the sensor groups in LibreHardwareMonitor's data.json -> sensor type.  Not
# every plural is the singular plus "s" ("Frequencies", "Small Data", "Noise Levels").
_GROUP_TYPES = {
    "temperatures": "temperature", "load": "load", "loads": "load", "fans": "fan", "powers": "power",
    "clocks": "clock", "voltages": "voltage", "controls": "control", "levels": "level", "data": "data",
    "small data": "smalldata", "smalldata": "smalldata", "throughput": "throughput", "throughputs": "throughput",
    "factors": "factor", "currents": "current", "frequencies": "frequency", "energies": "energy", "energy": "energy",
    "noise levels": "noise", "noise": "noise", "noises": "noise", "humidity": "humidity", "humidities": "humidity",
    "time spans": "timespan", "timespans": "timespan", "timespan": "timespan", "flows": "flow",
    "liquid flows": "flow", "conductivities": "conductivity", "conductivity": "conductivity",
}
# file name of the hardware's icon in data.json -> hardware type
_IMAGE_TYPES = (("hdd", "storage"), ("ssd", "storage"), ("cpu", "cpu"), ("nvidia", "gpu"), ("ati", "gpu"), ("amd", "gpu"),
                ("intel", "gpu"), ("gpu", "gpu"), ("mainboard", "motherboard"), ("ram", "memory"), ("nic", "network"),
                ("battery", "battery"))


def group_sensor_type(caption: str, sensor_type: Optional[str] = None) -> str:
    """The sensor type of a data.json group (pure; unit-tested).

    Newer LibreHardwareMonitor versions name the type on every sensor
    (``"Type": "Temperature"``); that wins over the group's caption.
    """
    if sensor_type:
        return str(sensor_type).strip().lower().replace(" ", "")
    t = (caption or "").strip().lower()
    if t in _GROUP_TYPES:
        return _GROUP_TYPES[t]
    return t[:-1] if t.endswith("s") and not t.endswith("ss") else t


def image_hardware_type(image: str) -> str:
    name = (image or "").lower().rsplit("/", 1)[-1]
    for needle, hw_type in _IMAGE_TYPES:
        if needle in name:
            return hw_type
    return ""


def read_pause(interval: float, elapsed: float, cap: float = 300.0) -> float:
    """Seconds to wait after a read that took ``elapsed`` (pure; unit-tested).

    A slow backend (SMART on a sleeping disk) is polled less often, but never
    so rarely that the readings would be considered lost.
    """
    return max(float(interval), min(float(elapsed) * 4, cap))


def stale_after(interval: float, last_pause: float) -> float:
    """Age at which readings are no longer trusted: clearly more than the wait before the next read."""
    return max(60.0, float(interval) * 4, float(last_pause) * 2 + 30.0)


class SensorFilter:
    def __init__(self, cfg: HardwareSensorsConfig):
        self.types = {t.lower() for t in cfg.sensor_types}
        self.include = [re.compile(p, re.I) for p in cfg.include]
        self.exclude = [re.compile(p, re.I) for p in cfg.exclude]
        self.max_sensors = cfg.max_sensors

    def accept(self, reading: HwReading) -> bool:
        label = f"{reading.hardware}/{reading.sensor_type}/{reading.name}"
        if self.include and not any(p.search(label) for p in self.include):
            return False
        if any(p.search(label) for p in self.exclude):
            return False
        if not self.include and reading.sensor_type not in self.types:
            return False
        return True


def _key_base(hardware: str, sensor_type: str, name: str) -> str:
    return slugify(f"{hardware}_{name}_{sensor_type}")[:60]


def _make_key(hardware: str, sensor_type: str, name: str, used: set[str]) -> str:
    base = _key_base(hardware, sensor_type, name)
    key = base
    i = 2
    while key in used:
        key = f"{base}_{i}"
        i += 1
    used.add(key)
    return key


def unique_keys(bases: list[str], identifiers: list[Optional[str]]) -> list[str]:
    """Sensor keys that do not depend on the order the sensors are listed in (pure; unit-tested).

    A sensor whose name is unique keeps its plain key (``cpu_package_temperature``),
    exactly as before.  Sensors sharing a name ("Temperature" of two NVMe drives
    with the same model name) used to be told apart by their position (``_2``),
    which moved when the order changed.  Now the one with the lowest
    LibreHardwareMonitor identifier keeps the plain key and every other one gets
    its identifier as the suffix (``..._nvme_1_temperature_0``); only without an
    identifier is the position used.
    """
    groups: dict[str, list[int]] = {}
    for i, base in enumerate(bases):
        groups.setdefault(base, []).append(i)
    keys = list(bases)
    used = set(bases)
    for base, members in groups.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda i: (identifiers[i] is None, str(identifiers[i] or ""), i))
        for i in ordered[1:]:
            ident = slugify(identifiers[i]) if identifiers[i] else ""
            key = f"{base}_{ident}"[:120] if ident else ""
            n = 2
            while not key or key in used:
                key = f"{base}_{n}"
                n += 1
            used.add(key)
            keys[i] = key
    return keys


# LibreHardwareMonitor before this version talks to the hardware through the WinRing0 driver
# (CVE-2020-14979: any local user can read and write physical memory and I/O ports through it);
# later versions use PawnIO instead.
LHM_SAFE_VERSION = (0, 9, 5)


def lhm_uses_winring0(version: Optional[tuple[int, ...]]) -> Optional[bool]:
    """Does this LibreHardwareMonitorLib version load WinRing0? None when unknown (pure; unit-tested)."""
    if not version:
        return None
    return tuple(version[:3]) < LHM_SAFE_VERSION


class LhmLibBackend:
    """LibreHardwareMonitorLib.dll via pythonnet (``pip install pythonnet``)."""

    name = "lhm"

    def __init__(self, cfg: HardwareSensorsConfig):
        import clr  # type: ignore  # pythonnet

        clr.AddReference(cfg.lhm_dll_path)
        from LibreHardwareMonitor.Hardware import Computer  # type: ignore

        self.cfg = cfg
        self._computer = Computer()
        self.version: Optional[tuple[int, ...]] = None
        try:
            v = self._computer.GetType().Assembly.GetName().Version
            self.version = (int(v.Major), int(v.Minor), int(v.Build))
        except Exception:  # noqa: BLE001
            pass
        if lhm_uses_winring0(self.version):
            log.warning("LibreHardwareMonitorLib %s loads the WinRing0 driver (CVE-2020-14979), which lets any local "
                        "user reach physical memory; update LibreHardwareMonitor to %s or later",
                        ".".join(map(str, self.version or ())), ".".join(map(str, LHM_SAFE_VERSION)))
        c = self._computer
        c.IsCpuEnabled = cfg.cpu
        c.IsGpuEnabled = cfg.gpu
        c.IsMotherboardEnabled = cfg.motherboard
        c.IsMemoryEnabled = cfg.memory
        c.IsStorageEnabled = cfg.storage
        c.IsControllerEnabled = cfg.controller
        c.IsNetworkEnabled = cfg.network
        for attr, enabled in (("IsPsuEnabled", cfg.psu), ("IsBatteryEnabled", cfg.battery)):
            if hasattr(c, attr):
                setattr(c, attr, enabled)
        c.Open()
        self._last_storage = 0.0

    def read(self) -> list[HwReading]:
        now = time.monotonic()
        update_storage = now - self._last_storage >= self.cfg.storage_interval_seconds
        readings: list[HwReading] = []
        identifiers: list[Optional[str]] = []
        for hw in self._computer.Hardware:
            self._collect(hw, readings, set(), update_storage, identifiers)
        if update_storage:
            self._last_storage = now
        for reading, key in zip(readings, unique_keys([r.key for r in readings], identifiers)):
            reading.key = key
        return readings

    def _collect(self, hw, readings, used, update_storage, identifiers: Optional[list] = None) -> None:
        """Append the sensors of ``hw`` (and its sub-hardware).  With ``identifiers`` the keys are
        left plain and each sensor's identifier is collected for ``unique_keys``."""
        hw_type = str(hw.HardwareType)
        # Storage (SMART) is slow, so it is only *refreshed* every storage interval.  Its
        # sensors are still collected every time (with the values of the last refresh) so
        # the set of readings stays the same from one poll to the next.
        if hw_type.lower() != "storage" or update_storage:
            try:
                hw.Update()
            except Exception as exc:  # noqa: BLE001
                log.debug("LHM update failed for %s: %s", hw.Name, exc)
                return
        for sensor in hw.Sensors:
            value = sensor.Value
            val = None if value is None else float(value)
            s_type = str(sensor.SensorType).lower()
            if identifiers is not None:
                key = _key_base(str(hw.Name), s_type, str(sensor.Name))
                try:
                    ident = getattr(sensor, "Identifier", None)
                    identifiers.append(str(ident) if ident is not None else None)
                except Exception:  # noqa: BLE001
                    identifiers.append(None)
            else:
                key = _make_key(str(hw.Name), s_type, str(sensor.Name), used)
            readings.append(
                HwReading(
                    key=key,
                    hardware=str(hw.Name), hardware_type=hw_type, sensor_type=s_type,
                    name=str(sensor.Name), value=None if val is None else round(val, 2),
                    unit=_UNIT_BY_TYPE.get(s_type, ""),
                )
            )
        for sub in hw.SubHardware:
            self._collect(sub, readings, used, update_storage, identifiers)

    def close(self) -> None:
        try:
            self._computer.Close()
        except Exception:  # noqa: BLE001
            pass


class LhmHttpBackend:
    """LibreHardwareMonitor / OpenHardwareMonitor web server ``data.json``."""

    name = "lhm_http"
    _VALUE_RE = re.compile(r"^\s*(-?[\d.,]+)\s*(.*)$")
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, cfg: HardwareSensorsConfig):
        self.url = cfg.lhm_http_url
        self.cfg = cfg
        self.read()  # raise early when unreachable

    def read(self) -> list[HwReading]:
        with urllib.request.urlopen(self.url, timeout=3) as resp:  # noqa: S310 - local URL from config
            raw = resp.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            # whatever answers on that port is not LibreHardwareMonitor; never buffer it all
            raise ValueError(f"sensor data larger than {self.MAX_BYTES} bytes")
        data = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise ValueError("unexpected sensor data")
        readings: list[HwReading] = []
        identifiers: list[Optional[str]] = []
        for machine in data.get("Children", []):
            for hw in machine.get("Children", []):
                self._walk_hw(hw, readings, identifiers)
        for reading, key in zip(readings, unique_keys([r.key for r in readings], identifiers)):
            reading.key = key
        return readings

    def _walk_hw(self, hw: dict[str, Any], readings, identifiers) -> None:
        hw_name = hw.get("Text", "?")
        for group in hw.get("Children", []):
            if group.get("Children") and not group.get("Value"):
                # either a sensor-type group ("Temperatures") or sub-hardware
                children = group["Children"]
                if children and children[0].get("Children"):
                    self._walk_hw(group, readings, identifiers)
                    continue
                for sensor in children:
                    if not isinstance(sensor, dict):
                        continue
                    s_type = group_sensor_type(str(group.get("Text", "")), sensor.get("Type"))
                    self._add(sensor, hw_name, str(hw.get("ImageURL", "") or ""), s_type, readings, identifiers)

    @staticmethod
    def _type_from_group(text: str) -> str:
        return group_sensor_type(text)

    def _add(self, sensor, hw_name, image, s_type, readings, identifiers) -> None:
        raw = str(sensor.get("Value", ""))
        m = self._VALUE_RE.match(raw)
        value = None
        unit = ""
        if m:
            try:
                value = float(m.group(1).replace(",", "."))
            except ValueError:
                value = None
            unit = m.group(2).strip()
        hw_type = image_hardware_type(image)
        if isinstance(identifiers, list):
            ident = sensor.get("SensorId")   # newer versions: the stable identifier ("/amdcpu/0/temperature/2")
            identifiers.append(str(ident) if isinstance(ident, str) and ident else None)
            key = _key_base(hw_name, s_type, sensor.get("Text", "?"))   # made unique by read()
        else:   # a set of keys already used: numbered in order
            key = _make_key(hw_name, s_type, sensor.get("Text", "?"), identifiers)
        readings.append(
            HwReading(
                key=key, hardware=hw_name,
                hardware_type=hw_type, sensor_type=s_type, name=sensor.get("Text", "?"), value=value,
                unit=unit or _UNIT_BY_TYPE.get(s_type, ""),
            )
        )

    def close(self) -> None:
        pass


class HardwareSensors:
    """Owns a backend and polls it from a worker thread so slow reads never block the main loop."""

    def __init__(self, cfg: HardwareSensorsConfig):
        self.cfg = cfg
        self.filter = SensorFilter(cfg)
        self.backend = None
        self._lock = threading.Lock()
        self._latest: list[HwReading] = []
        self._latest_at = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error_count = 0
        self._pause = 0.0   # the wait before the next read; a slow read stretches it

    def start(self) -> None:
        if not self.cfg.enabled or self.cfg.backend == "none":
            log.info("hardware sensors disabled")
            return
        # The backend is made on the worker: opening the hardware (LibreHardwareMonitorLib) takes
        # seconds, and probing the app's web server up to its timeout - never on the service's
        # start.  The worker runs even without a backend: the LibreHardwareMonitor app (lhm_http)
        # usually starts after the service, at logon, so creation is retried.
        self._thread = threading.Thread(target=self._run, name="hwsensors", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.backend = self._create_backend()
        except Exception:  # noqa: BLE001
            log.exception("hardware sensor backend could not be created")
        self._loop()

    RETRY_BACKEND_SECONDS = 60.0

    def _create_backend(self, quiet: bool = False):
        order = {"auto": ["lhm", "lhm_http"], "lhm": ["lhm"], "lhm_http": ["lhm_http"]}.get(self.cfg.backend, [])
        for name in order:
            try:
                backend = LhmLibBackend(self.cfg) if name == "lhm" else LhmHttpBackend(self.cfg)
                log.info("hardware sensor backend: %s", name)
                return backend
            except Exception as exc:  # noqa: BLE001
                log.log(logging.DEBUG if quiet else logging.INFO, "hardware sensor backend %s unavailable: %s", name, exc)
        if not quiet:
            log.warning("no hardware sensor backend available yet; CPU/mainboard temperatures will be missing "
                        "(retrying every %d s)", int(self.RETRY_BACKEND_SECONDS))
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.backend is None:
                if self._stop.wait(self.RETRY_BACKEND_SECONDS):
                    return
                try:
                    self.backend = self._create_backend(quiet=True)
                except Exception:  # noqa: BLE001
                    log.debug("hardware sensor backend retry failed", exc_info=True)
                continue
            started = time.monotonic()
            try:
                readings = [r for r in self.backend.read() if self.filter.accept(r)]
                readings = readings[: self.filter.max_sensors]
                with self._lock:
                    self._latest = readings
                    self._latest_at = time.monotonic()
                self._error_count = 0
            except Exception as exc:  # noqa: BLE001
                self._error_count += 1
                if self._error_count in (1, 10, 100):
                    log.warning("hardware sensor read failed (%d): %s", self._error_count, exc)
            elapsed = time.monotonic() - started
            if elapsed > 2.0:
                log.debug("hardware sensor read took %.1fs", elapsed)
            # The readings must stay valid for as long as this wait: the staleness limit in
            # latest() follows it.  (It did not, and one slow SMART refresh blanked every sensor.)
            self._pause = read_pause(self.cfg.interval_seconds, elapsed)
            self._stop.wait(self._pause)

    def latest(self) -> list[HwReading]:
        with self._lock:
            if time.monotonic() - self._latest_at > stale_after(self.cfg.interval_seconds, self._pause):
                return []
            return list(self._latest)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.backend is not None:
            self.backend.close()
