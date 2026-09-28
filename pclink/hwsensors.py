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


def _make_key(hardware: str, sensor_type: str, name: str, used: set[str]) -> str:
    base = slugify(f"{hardware}_{name}_{sensor_type}")[:60]
    key = base
    i = 2
    while key in used:
        key = f"{base}_{i}"
        i += 1
    used.add(key)
    return key


class LhmLibBackend:
    """LibreHardwareMonitorLib.dll via pythonnet (``pip install pythonnet``)."""

    name = "lhm"

    def __init__(self, cfg: HardwareSensorsConfig):
        import clr  # type: ignore  # pythonnet

        clr.AddReference(cfg.lhm_dll_path)
        from LibreHardwareMonitor.Hardware import Computer  # type: ignore

        self.cfg = cfg
        self._computer = Computer()
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
        used: set[str] = set()
        for hw in self._computer.Hardware:
            self._collect(hw, readings, used, update_storage)
        if update_storage:
            self._last_storage = now
        return readings

    def _collect(self, hw, readings, used, update_storage) -> None:
        hw_type = str(hw.HardwareType)
        if hw_type.lower() == "storage" and not update_storage:
            return
        try:
            hw.Update()
        except Exception as exc:  # noqa: BLE001
            log.debug("LHM update failed for %s: %s", hw.Name, exc)
            return
        for sensor in hw.Sensors:
            value = sensor.Value
            val = None if value is None else float(value)
            s_type = str(sensor.SensorType).lower()
            readings.append(
                HwReading(
                    key=_make_key(str(hw.Name), s_type, str(sensor.Name), used),
                    hardware=str(hw.Name), hardware_type=hw_type, sensor_type=s_type,
                    name=str(sensor.Name), value=None if val is None else round(val, 2),
                    unit=_UNIT_BY_TYPE.get(s_type, ""),
                )
            )
        for sub in hw.SubHardware:
            self._collect(sub, readings, used, update_storage)

    def close(self) -> None:
        try:
            self._computer.Close()
        except Exception:  # noqa: BLE001
            pass


class LhmHttpBackend:
    """LibreHardwareMonitor / OpenHardwareMonitor web server ``data.json``."""

    name = "lhm_http"
    _VALUE_RE = re.compile(r"^\s*(-?[\d.,]+)\s*(.*)$")

    def __init__(self, cfg: HardwareSensorsConfig):
        self.url = cfg.lhm_http_url
        self.cfg = cfg
        self.read()  # raise early when unreachable

    def read(self) -> list[HwReading]:
        with urllib.request.urlopen(self.url, timeout=3) as resp:  # noqa: S310 - local URL from config
            data = json.loads(resp.read().decode("utf-8", "replace"))
        readings: list[HwReading] = []
        used: set[str] = set()
        for machine in data.get("Children", []):
            for hw in machine.get("Children", []):
                self._walk_hw(hw, readings, used)
        return readings

    def _walk_hw(self, hw: dict[str, Any], readings, used) -> None:
        hw_name = hw.get("Text", "?")
        for group in hw.get("Children", []):
            if group.get("Children") and not group.get("Value"):
                # either a sensor-type group ("Temperatures") or sub-hardware
                children = group["Children"]
                if children and children[0].get("Children"):
                    self._walk_hw(group, readings, used)
                    continue
                s_type = self._type_from_group(group.get("Text", ""))
                for sensor in children:
                    self._add(sensor, hw_name, hw.get("ImageURL", ""), s_type, readings, used)

    @staticmethod
    def _type_from_group(text: str) -> str:
        t = text.lower().rstrip("s")
        return {"temperature": "temperature", "load": "load", "fan": "fan", "power": "power", "clock": "clock",
                "voltage": "voltage", "control": "control", "level": "level", "data": "data", "throughput": "throughput",
                "factor": "factor", "current": "current"}.get(t, t)

    def _add(self, sensor, hw_name, image, s_type, readings, used) -> None:
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
        hw_type = "storage" if "hdd" in image or "ssd" in image else ("cpu" if "cpu" in image else "gpu" if "gpu" in image else "motherboard" if "mainboard" in image else "")
        readings.append(
            HwReading(
                key=_make_key(hw_name, s_type, sensor.get("Text", "?"), used), hardware=hw_name,
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

    def start(self) -> None:
        if not self.cfg.enabled or self.cfg.backend == "none":
            log.info("hardware sensors disabled")
            return
        self.backend = self._create_backend()
        if self.backend is None:
            return
        self._thread = threading.Thread(target=self._loop, name="hwsensors", daemon=True)
        self._thread.start()

    def _create_backend(self):
        order = {"auto": ["lhm", "lhm_http"], "lhm": ["lhm"], "lhm_http": ["lhm_http"]}.get(self.cfg.backend, [])
        for name in order:
            try:
                backend = LhmLibBackend(self.cfg) if name == "lhm" else LhmHttpBackend(self.cfg)
                log.info("hardware sensor backend: %s", name)
                return backend
            except Exception as exc:  # noqa: BLE001
                log.info("hardware sensor backend %s unavailable: %s", name, exc)
        log.warning("no hardware sensor backend available; CPU/mainboard temperatures will be missing")
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
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
            self._stop.wait(max(self.cfg.interval_seconds, elapsed * 4))

    def latest(self) -> list[HwReading]:
        with self._lock:
            if time.monotonic() - self._latest_at > max(60.0, self.cfg.interval_seconds * 4):
                return []
            return list(self._latest)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.backend is not None:
            self.backend.close()
