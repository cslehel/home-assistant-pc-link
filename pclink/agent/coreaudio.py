"""Core Audio (WASAPI) through comtypes: output peak meter and endpoint volume.

Used by the agent for two things:

* ``peak()`` - is anything audible right now?  (keeps a film from looking idle)
* ``volume()`` / ``set_volume()`` / ``muted()`` / ``set_mute()`` - the
  session's default output device, exposed to Home Assistant.

The interfaces are declared by hand (the same vtables pycaw uses) so the only
dependency is ``comtypes``, which the agent already needs for UI Automation.
Everything degrades to ``None``/no-ops when comtypes or the audio stack is not
available (e.g. a session without an audio device).
"""
from __future__ import annotations

import logging
import sys
from ctypes import POINTER, c_float, c_uint32
from typing import Optional

log = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

try:  # pragma: no cover - only importable on Windows with comtypes
    import comtypes  # type: ignore
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # type: ignore
    from ctypes import wintypes

    class IAudioMeterInformation(IUnknown):
        _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
        _methods_ = (
            COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "pfPeak")),
            COMMETHOD([], HRESULT, "GetMeteringChannelCount", (["out"], POINTER(c_uint32), "pnChannelCount")),
            COMMETHOD([], HRESULT, "GetChannelsPeakValues", (["in"], c_uint32, "u32ChannelCount"), (["out"], POINTER(c_float), "afPeakValues")),
            COMMETHOD([], HRESULT, "QueryHardwareSupport", (["out"], POINTER(c_uint32), "pdwHardwareSupportMask")),
        )

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = (
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], POINTER(IUnknown), "pNotify")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], POINTER(IUnknown), "pNotify")),
            COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint32), "pnChannelCount")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float, "fLevelDB"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float, "fLevel"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint32, "nChannel"), (["in"], c_float, "fLevelDB"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint32, "nChannel"), (["in"], c_float, "fLevel"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint32, "nChannel"), (["out"], POINTER(c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint32, "nChannel"), (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetMute", (["in"], wintypes.BOOL, "bMute"), (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(wintypes.BOOL), "pbMute")),
            COMMETHOD([], HRESULT, "GetVolumeStepInfo", (["out"], POINTER(c_uint32), "pnStep"), (["out"], POINTER(c_uint32), "pnStepCount")),
            COMMETHOD([], HRESULT, "VolumeStepUp", (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "VolumeStepDown", (["in"], POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "QueryHardwareSupport", (["out"], POINTER(c_uint32), "pdwHardwareSupportMask")),
            COMMETHOD([], HRESULT, "GetVolumeRange", (["out"], POINTER(c_float), "pflVolumeMindB"), (["out"], POINTER(c_float), "pflVolumeMaxdB"), (["out"], POINTER(c_float), "pflVolumeIncrementdB")),
        )

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = (
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], wintypes.DWORD, "dwClsCtx"),
                      (["in"], POINTER(wintypes.DWORD), "pActivationParams"), (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], wintypes.DWORD, "stgmAccess"), (["out"], POINTER(POINTER(IUnknown)), "ppProperties")),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(wintypes.LPWSTR), "ppstrId")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(wintypes.DWORD), "pdwState")),
        )

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = (
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], wintypes.DWORD, "dataFlow"), (["in"], wintypes.DWORD, "dwStateMask"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppDevices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], wintypes.DWORD, "dataFlow"), (["in"], wintypes.DWORD, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
            COMMETHOD([], HRESULT, "GetDevice", (["in"], wintypes.LPCWSTR, "pwstrId"), (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], POINTER(IUnknown), "pClient")),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], POINTER(IUnknown), "pClient")),
        )

    CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    _COMTYPES = True
except Exception:  # noqa: BLE001
    _COMTYPES = False

E_RENDER = 0
E_MULTIMEDIA = 1
CLSCTX_ALL = 23


class CoreAudio:
    """Default render endpoint of *this* session (each ASTER seat has its own)."""

    def __init__(self, enabled: bool = True):
        self.available = bool(_IS_WIN and enabled and _COMTYPES)
        self._meter = None
        self._volume = None
        self._failures = 0

    # -- device acquisition ------------------------------------------------------------
    def _endpoint(self):
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        return enum.GetDefaultAudioEndpoint(E_RENDER, E_MULTIMEDIA)

    def _ensure(self) -> bool:
        if not self.available:
            return False
        if self._meter is not None and self._volume is not None:
            return True
        try:
            dev = self._endpoint()
            meter = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            self._meter = meter.QueryInterface(IAudioMeterInformation)
            vol = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume = vol.QueryInterface(IAudioEndpointVolume)
            self._failures = 0
            return True
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            if self._failures in (1, 10):
                log.info("audio endpoint unavailable (%s); volume/meter disabled for now", exc)
            self._meter = self._volume = None
            return False

    def _reset(self) -> None:
        self._meter = self._volume = None

    # -- queries ----------------------------------------------------------------------------
    def peak(self) -> Optional[float]:
        if not self._ensure():
            return None
        try:
            return float(self._meter.GetPeakValue())
        except Exception:  # noqa: BLE001
            self._reset()
            return None

    def active(self, threshold: float = 0.01) -> bool:
        peak = self.peak()
        return bool(peak is not None and peak > threshold)

    def volume(self) -> Optional[int]:
        if not self._ensure():
            return None
        try:
            return int(round(float(self._volume.GetMasterVolumeLevelScalar()) * 100))
        except Exception:  # noqa: BLE001
            self._reset()
            return None

    def muted(self) -> Optional[bool]:
        if not self._ensure():
            return None
        try:
            return bool(self._volume.GetMute())
        except Exception:  # noqa: BLE001
            self._reset()
            return None

    # -- control -------------------------------------------------------------------------------
    def set_volume(self, level: int) -> bool:
        if not self._ensure():
            return False
        try:
            self._volume.SetMasterVolumeLevelScalar(max(0.0, min(100.0, float(level))) / 100.0, None)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("set volume failed: %s", exc)
            self._reset()
            return False

    def set_mute(self, muted: bool) -> bool:
        if not self._ensure():
            return False
        try:
            self._volume.SetMute(1 if muted else 0, None)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("set mute failed: %s", exc)
            self._reset()
            return False
