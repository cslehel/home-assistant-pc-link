"""GPU utilisation backends: NVIDIA NVML first, Windows PDH GPU counters as fallback.

``read()`` is called from the telemetry module's own thread (never the main
loop).  The headline figures describe the *busiest* GPU; with more than one,
``gpus`` lists them all.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

_MB = 1024 ** 2

# "pid_13444_luid_0x00000000_0x0000F91B_phys_0_eng_3_engtype_VideoDecode"
_ENGINE_RE = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_engtype_(.+)$", re.I)
_ADAPTER_RE = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)", re.I)


def aggregate_gpu_engines(values: dict[str, float]) -> list[dict[str, Any]]:
    """Per-adapter load from "GPU Engine" counter instances (pure; unit-tested).

    The way Task Manager does it: per adapter and engine the utilisation of all
    processes is summed, and the adapter's load is its busiest engine.  Adapters
    are never added together - an idle integrated GPU next to a busy graphics
    card is two GPUs, not one at the sum.
    """
    engines: dict[str, dict[tuple[str, str], float]] = {}
    for instance, value in values.items():
        m = _ENGINE_RE.search(str(instance))
        if not m:
            continue
        adapter = f"{m.group(1).lower()}_{m.group(2)}"
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number < 0:   # NaN or nonsense
            continue
        per_engine = engines.setdefault(adapter, {})
        key = (m.group(3), m.group(4))
        per_engine[key] = per_engine.get(key, 0.0) + number
    out = []
    for adapter in sorted(engines):
        (_eng, engtype), load = max(engines[adapter].items(), key=lambda kv: kv[1])
        out.append({"adapter": adapter, "load": round(min(100.0, load), 1), "engine": engtype})
    return out


def busiest(gpus: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The GPU the headline sensors describe: the one with the highest load (pure)."""
    if not gpus:
        return None
    return max(gpus, key=lambda g: (g.get("load") is not None, g.get("load") or 0.0))


class NvmlGpu:
    """NVIDIA GPUs via nvidia-ml-py (module name ``pynvml``)."""

    REINIT_SECONDS = 60.0      # after losing the driver (an update replaces it), try again this often
    FAILURES_BEFORE_REINIT = 3

    def __init__(self, nvml=None):
        if nvml is None:
            import pynvml as nvml  # type: ignore

        self._nvml = nvml
        self._error = getattr(nvml, "NVMLError", Exception)
        self._handles: list[tuple[Any, str]] = []
        self._failures = 0
        self._retry_at = 0.0
        self._open()
        self.name = self._handles[0][1]

    def _open(self) -> None:
        nv = self._nvml
        nv.nvmlInit()
        handles = []
        for i in range(int(nv.nvmlDeviceGetCount())):
            try:
                h = nv.nvmlDeviceGetHandleByIndex(i)
                name = nv.nvmlDeviceGetName(h)
            except self._error:
                continue
            handles.append((h, name.decode() if isinstance(name, bytes) else str(name)))
        if not handles:
            raise RuntimeError("NVML reports no GPU")
        self._handles = handles
        self._failures = 0

    def _read_one(self, h, name: str) -> dict[str, Any]:
        nv = self._nvml
        out: dict[str, Any] = {"name": name}
        try:
            util = nv.nvmlDeviceGetUtilizationRates(h)
            out["load"] = float(util.gpu)
        except self._error:
            pass
        try:
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            out["memory_used_mb"] = round(mem.used / _MB)
            out["memory_total_mb"] = round(mem.total / _MB)
            out["memory_percent"] = round(mem.used * 100.0 / mem.total, 1) if mem.total else None
        except self._error:
            pass
        try:
            out["temperature"] = float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU))
        except self._error:
            pass
        try:
            out["power"] = round(nv.nvmlDeviceGetPowerUsage(h) / 1000.0, 1)
        except self._error:
            pass
        return out

    def read(self) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        if not self._handles:
            if now < self._retry_at:
                return None
            try:
                self._open()
                log.info("NVML is back (%d GPU)", len(self._handles))
            except Exception as exc:  # noqa: BLE001
                self._retry_at = now + self.REINIT_SECONDS
                log.debug("NVML still unavailable: %s", exc)
                return None
        gpus = [self._read_one(h, name) for h, name in self._handles]
        if not any(len(g) > 1 for g in gpus):
            # Nothing could be read at all.  After a driver update every call fails until the
            # library is initialised again; without this the sensors stayed blank until a restart.
            self._failures += 1
            if self._failures >= self.FAILURES_BEFORE_REINIT:
                log.info("NVML stopped answering; initialising it again")
                self.close()
                self._handles = []
                self._retry_at = now + self.REINIT_SECONDS
            return None
        self._failures = 0
        out = dict(busiest(gpus) or gpus[0])
        if len(gpus) > 1:
            out["gpus"] = gpus
        return out

    def close(self) -> None:
        try:
            self._nvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass


class PdhGpu:
    """Vendor-neutral GPU load from the Windows "GPU Engine" performance counters.

    One wildcard counter covers every process and engine, and Windows expands
    it again on every collection - so a game started a second ago is counted
    at once (the query is still rebuilt every few hours, as a safeguard against
    the instances piling up over a long uptime).  The counter is added by its
    English name, which works on every display language.
    """

    ENGINE_PATH = r"\GPU Engine(*)\Utilization Percentage"
    MEMORY_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"
    REBUILD_AFTER_ERROR_SECONDS = 60.0
    # The wildcard counter gains an instance for every process that ever used the GPU; the
    # query is built afresh now and then so a PC that runs for weeks does not keep them all.
    REBUILD_EVERY_SECONDS = 6 * 3600.0

    def __init__(self, pdh=None):
        if pdh is None:
            import win32pdh as pdh  # type: ignore

        self._pdh = pdh
        self.name = "GPU (performance counters)"
        self._query = None
        self._engine = None
        self._memory = None
        self._built_at = 0.0
        self._last: Optional[dict[str, Any]] = None
        self._build()

    def _add(self, query, path: str):
        add = getattr(self._pdh, "AddEnglishCounter", None) or self._pdh.AddCounter
        return add(query, path)

    def _build(self) -> None:
        pdh = self._pdh
        self.close()
        self._built_at = time.monotonic()
        query = pdh.OpenQuery()
        try:
            self._engine = self._add(query, self.ENGINE_PATH)
        except Exception:
            try:
                pdh.CloseQuery(query)
            except Exception:  # noqa: BLE001
                pass
            raise
        try:
            self._memory = self._add(query, self.MEMORY_PATH)
        except Exception:  # noqa: BLE001 - the load is what matters
            self._memory = None
        self._query = query
        try:
            pdh.CollectQueryData(query)  # first sample; values need two samples
        except Exception:  # noqa: BLE001
            pass

    @property
    def available(self) -> bool:
        return self._engine is not None

    def _array(self, counter) -> dict[str, float]:
        try:
            return dict(self._pdh.GetFormattedCounterArray(counter, self._pdh.PDH_FMT_DOUBLE))
        except Exception:  # noqa: BLE001 - no instance at the moment, or no second sample yet
            return {}

    def read(self) -> Optional[dict[str, Any]]:
        pdh = self._pdh
        if self._query is None:
            if time.monotonic() - self._built_at < self.REBUILD_AFTER_ERROR_SECONDS:
                return None
            try:
                self._build()
            except Exception as exc:  # noqa: BLE001
                log.debug("GPU counters still unavailable: %s", exc)
            return None
        if time.monotonic() - self._built_at >= self.REBUILD_EVERY_SECONDS:
            try:
                self._build()   # the next read has values again; this one keeps the last reading
            except Exception as exc:  # noqa: BLE001
                log.debug("GPU counters could not be rebuilt: %s", exc)
                self.close()   # tried again after a pause
            return dict(self._last) if self._last else None
        try:
            pdh.CollectQueryData(self._query)
        except Exception:  # noqa: BLE001
            self.close()   # rebuilt after a pause
            self._last = None
            return None
        gpus = aggregate_gpu_engines(self._array(self._engine))
        if not gpus:
            # no engine instance right now (or the first sample): keep the last reading for
            # this one snapshot instead of making the sensor disappear
            last, self._last = self._last, None
            return dict(last) if last else None
        if self._memory is not None:
            for instance, used in self._array(self._memory).items():
                m = _ADAPTER_RE.search(str(instance))
                key = f"{m.group(1).lower()}_{m.group(2)}" if m else None
                for g in gpus:
                    if g["adapter"] == key:
                        g["memory_used_mb"] = round(float(used) / _MB)
        best = dict(busiest(gpus) or gpus[0])
        out: dict[str, Any] = {"name": self.name, "load": best["load"]}
        if "memory_used_mb" in best:
            out["memory_used_mb"] = best["memory_used_mb"]
        if len(gpus) > 1:
            out["gpus"] = gpus
        self._last = out
        return dict(out)

    def close(self) -> None:
        if self._query is not None:
            try:
                self._pdh.CloseQuery(self._query)
            except Exception:  # noqa: BLE001
                pass
        self._query = None
        self._engine = None
        self._memory = None


class GpuReader:
    """NVML while it answers, the Windows counters otherwise."""

    def __init__(self, nvml: Optional[NvmlGpu], pdh: Optional[PdhGpu]):
        self.nvml = nvml
        self.pdh = pdh
        self._pdh_tried = pdh is not None
        self.name = nvml.name if nvml is not None else (pdh.name if pdh is not None else "GPU")

    def read(self) -> Optional[dict[str, Any]]:
        if self.nvml is not None:
            reading = self.nvml.read()
            if reading and reading.get("load") is not None:
                return reading
            if not self._pdh_tried:
                # NVML went away (driver update): the counters keep the load sensor alive meanwhile
                self._pdh_tried = True
                try:
                    self.pdh = PdhGpu()
                except Exception as exc:  # noqa: BLE001
                    log.debug("PDH GPU counters unavailable: %s", exc)
            fallback = self.pdh.read() if self.pdh is not None else None
            if fallback and reading:
                return dict(fallback, **{k: v for k, v in reading.items() if v is not None})
            return fallback or reading
        return self.pdh.read() if self.pdh is not None else None

    def close(self) -> None:
        for backend in (self.nvml, self.pdh):
            if backend is not None:
                backend.close()


def detect_gpu():
    """Return the best available GPU backend or ``None``."""
    try:
        gpu = NvmlGpu()
        log.info("GPU backend: NVML (%s)", ", ".join(name for _h, name in gpu._handles))
        return GpuReader(gpu, None)
    except Exception as exc:  # noqa: BLE001
        log.debug("NVML unavailable: %s", exc)
    try:
        gpu = PdhGpu()
        if gpu.available:
            log.info("GPU backend: Windows performance counters")
            return GpuReader(None, gpu)
        gpu.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("PDH GPU counters unavailable: %s", exc)
    log.info("No GPU backend available (install nvidia-ml-py for NVIDIA cards, or enable LibreHardwareMonitor)")
    return None
