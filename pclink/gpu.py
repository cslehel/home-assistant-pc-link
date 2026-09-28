"""GPU utilisation backends: NVIDIA NVML first, Windows PDH GPU counters as fallback."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger(__name__)


class NvmlGpu:
    """NVIDIA GPUs via nvidia-ml-py (module name ``pynvml``)."""

    def __init__(self):
        import pynvml  # type: ignore

        self._nvml = pynvml
        pynvml.nvmlInit()
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(self._handle)
        self.name = name.decode() if isinstance(name, bytes) else str(name)

    def read(self) -> dict[str, Any]:
        nv = self._nvml
        h = self._handle
        out: dict[str, Any] = {"name": self.name}
        try:
            util = nv.nvmlDeviceGetUtilizationRates(h)
            out["load"] = float(util.gpu)
        except nv.NVMLError:
            pass
        try:
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            out["memory_used_mb"] = round(mem.used / 1024 ** 2)
            out["memory_total_mb"] = round(mem.total / 1024 ** 2)
            out["memory_percent"] = round(mem.used * 100.0 / mem.total, 1) if mem.total else None
        except nv.NVMLError:
            pass
        try:
            out["temperature"] = float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU))
        except nv.NVMLError:
            pass
        try:
            out["power"] = round(nv.nvmlDeviceGetPowerUsage(h) / 1000.0, 1)
        except nv.NVMLError:
            pass
        return out

    def close(self) -> None:
        try:
            self._nvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass


class PdhGpu:
    """Vendor-neutral GPU load from the Windows "GPU Engine" performance counters.

    This mirrors what Task Manager shows: per engine type the utilisation of all
    processes is summed and the busiest engine type is reported.  Counter
    instances come and go with processes, so the query is rebuilt periodically.
    """

    REBUILD_SECONDS = 60.0

    def __init__(self):
        import win32pdh  # type: ignore

        self._pdh = win32pdh
        self.name = "GPU (performance counters)"
        self._query = None
        self._counters: list[tuple[str, Any]] = []
        self._built_at = 0.0
        self._build()

    def _build(self) -> None:
        pdh = self._pdh
        self.close()
        query = pdh.OpenQuery()
        counters = []
        try:
            paths = pdh.ExpandCounterPath(r"\GPU Engine(*)\Utilization Percentage")
        except Exception as exc:  # noqa: BLE001
            log.debug("GPU Engine counters unavailable: %s", exc)
            paths = []
        for path in paths:
            try:
                idx = path.find("engtype_")
                engtype = path[idx + 8:].rstrip(")\\Utilization Percentage") if idx >= 0 else "?"
                engtype = engtype.split(")")[0]
                counters.append((engtype, pdh.AddCounter(query, path)))
            except Exception:  # noqa: BLE001
                continue
        self._query = query
        self._counters = counters
        self._built_at = time.monotonic()
        try:
            pdh.CollectQueryData(query)  # first sample; values need two samples
        except Exception:  # noqa: BLE001
            pass

    def read(self) -> Optional[dict[str, Any]]:
        pdh = self._pdh
        if self._query is None or time.monotonic() - self._built_at > self.REBUILD_SECONDS:
            self._build()
            return None
        try:
            pdh.CollectQueryData(self._query)
        except Exception:  # noqa: BLE001
            return None
        totals: dict[str, float] = {}
        for engtype, counter in self._counters:
            try:
                _, value = pdh.GetFormattedCounterValue(counter, pdh.PDH_FMT_DOUBLE)
            except Exception:  # noqa: BLE001
                continue
            totals[engtype] = totals.get(engtype, 0.0) + float(value)
        if not totals:
            return None
        load = min(100.0, max(totals.values()))
        return {"name": self.name, "load": round(load, 1)}

    def close(self) -> None:
        if self._query is not None:
            try:
                self._pdh.CloseQuery(self._query)
            except Exception:  # noqa: BLE001
                pass
        self._query = None
        self._counters = []


def detect_gpu():
    """Return the best available GPU backend or ``None``."""
    try:
        gpu = NvmlGpu()
        log.info("GPU backend: NVML (%s)", gpu.name)
        return gpu
    except Exception as exc:  # noqa: BLE001
        log.debug("NVML unavailable: %s", exc)
    try:
        gpu = PdhGpu()
        if gpu._counters:
            log.info("GPU backend: Windows performance counters")
            return gpu
        gpu.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("PDH GPU counters unavailable: %s", exc)
    log.info("No GPU backend available (install nvidia-ml-py for NVIDIA cards, or enable LibreHardwareMonitor)")
    return None
