"""Advertise the local API on the LAN with mDNS / DNS-SD (``_pclink._tcp.local.``).

The native Home Assistant integration (``custom_components/pclink``) listens
for this service type and offers the PC for setup with one click.  Optional:
needs the ``zeroconf`` package (in ``requirements-windows.txt``); without it,
or when the API is bound to loopback only, nothing is announced and the PC
can still be added by host name / IP.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Optional

from .config import ApiConfig
from .util import slugify

log = logging.getLogger(__name__)

SERVICE_TYPE = "_pclink._tcp.local."
LOOPBACK = ("127.0.0.1", "localhost", "::1")
REFRESH_SECONDS = 60.0   # how often the announced addresses are compared with the PC's current ones

# Adapters Home Assistant on the LAN cannot reach the PC through: virtual switches of VMs and
# WSL, container bridges, VPN overlays.  (A Hyper-V *external* switch - "vEthernet (<name>)" -
# carries the real LAN connection, so only the internal ones are listed.)
_VIRTUAL_ADAPTER_HINTS = ("vethernet (wsl", "vethernet (default switch", "virtualbox", "vmware", "vmnet",
                          "tailscale", "zerotier", "docker", "vboxnet", "loopback", "npcap", "wireguard", "wsl")


# The same, by the adapter's description (Windows).  A VPN's adapter is often named just
# "Local Area Connection" (OpenVPN's TAP) or after the VPN; its description gives it away.
_VIRTUAL_DESCRIPTION_HINTS = ("tap-windows", "openvpn", "wintun", "wireguard", "tailscale", "zerotier", "virtualbox",
                              "vmware", "cloudflare", "anyconnect", "globalprotect", "pangp", "fortinet", "fortissl",
                              "nordlynx", "mullvad", "protonvpn", "hamachi", "radmin vpn", "npcap", "loopback",
                              "wan miniport", "kernel debug", "wi-fi direct", "bluetooth")
# IfType values that are never a plain LAN connection: software loopback, PPP, tunnels, the
# "proprietary virtual" type of Wintun and other VPN drivers
_VIRTUAL_IF_TYPES = {23, 24, 53, 131}


def virtual_adapter(name: str, description: str = "", if_type: int = 0) -> bool:
    """Is this a virtual / VPN adapter whose address is no use to announce? (pure; unit-tested)

    ``description`` and ``if_type`` come from Windows (:func:`_adapter_details`), where known."""
    lname = (name or "").strip().lower()
    ldesc = (description or "").strip().lower()
    return any(hint in lname for hint in _VIRTUAL_ADAPTER_HINTS) or lname == "lo" or lname.startswith("br-") or \
        (lname.startswith("veth") and not lname.startswith("vethernet")) or \
        any(hint in ldesc for hint in _VIRTUAL_DESCRIPTION_HINTS) or if_type in _VIRTUAL_IF_TYPES


def _adapter_details() -> dict[str, tuple[str, int]]:
    """Windows: adapter friendly name (as psutil names it) -> (description, IfType); {} elsewhere
    or when it cannot be read.  From ``GetAdaptersAddresses``; only the start of each record is read."""
    import sys

    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    class _Adapter(ctypes.Structure):
        pass

    _Adapter._fields_ = [   # IP_ADAPTER_ADDRESSES, up to IfType (the layout of every Windows version since XP)
        ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD), ("Next", ctypes.POINTER(_Adapter)),
        ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p), ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.ULONG), ("Flags", wintypes.ULONG), ("Mtu", wintypes.ULONG),
        ("IfType", wintypes.DWORD)]
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")
        get = iphlpapi.GetAdaptersAddresses
        get.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
        get.restype = wintypes.ULONG
        # GAA_FLAG_SKIP_ANYCAST | SKIP_MULTICAST | SKIP_DNS_SERVER; AF_UNSPEC
        flags = 0x2 | 0x4 | 0x8
        size = wintypes.ULONG(32 * 1024)
        for _attempt in range(4):
            buf = ctypes.create_string_buffer(size.value)
            rc = get(0, flags, None, buf, ctypes.byref(size))
            if rc == 111:   # ERROR_BUFFER_OVERFLOW: size now holds what is needed
                continue
            if rc != 0:
                return {}
            details: dict[str, tuple[str, int]] = {}
            node = ctypes.cast(buf, ctypes.POINTER(_Adapter))
            while node:
                item = node.contents
                if item.FriendlyName:
                    details[item.FriendlyName] = (item.Description or "", int(item.IfType))
                node = item.Next
            return details
    except Exception:  # noqa: BLE001 - only a refinement of the name check
        log.debug("adapter descriptions could not be read", exc_info=True)
    return {}


def announceable(address: str) -> bool:
    """Not loopback, link-local or unset (pure; unit-tested)."""
    return bool(address) and not address.startswith(("127.", "169.254.", "0."))


def lan_addresses(bind_host: str) -> list[str]:
    """IPv4 addresses to announce for a given bind address.

    Virtual and VPN adapters (WSL, VirtualBox, VMware, Docker, Tailscale,
    ZeroTier ...) and adapters that are down are left out - unless nothing else
    is left, so a PC reachable only that way is still announced.
    """
    if bind_host and bind_host not in ("0.0.0.0", "::", "") and bind_host not in LOOPBACK:
        return [bind_host]
    found: list[str] = []
    fallback: list[str] = []
    try:
        import psutil

        try:
            stats = psutil.net_if_stats()
        except Exception:  # noqa: BLE001
            stats = {}
        details = _adapter_details()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is not None and not st.isup:
                continue
            description, if_type = details.get(name, ("", 0))
            for a in addrs:
                if a.family == socket.AF_INET and announceable(a.address):
                    (fallback if virtual_adapter(name, description, if_type) else found).append(a.address)
    except Exception:  # noqa: BLE001
        pass
    if not found:
        found = fallback
    if not found:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127."):
                    found.append(ip)
        except OSError:
            pass
    return sorted(set(found))


class MdnsAdvertiser:
    """Registers (and on stop unregisters) the ``_pclink._tcp`` service."""

    def __init__(self, cfg: ApiConfig, device_id: str, device_name: str, addresses: Optional[list[str]] = None):
        self.cfg = cfg
        self.device_id = device_id
        self.device_name = device_name
        self._addresses = addresses
        self._zc: Any = None
        self._info: Any = None
        self._announced: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def active(self) -> bool:
        return self._zc is not None

    def properties(self) -> dict[str, str]:
        # no version number: it would tell everything on the LAN which release (and its known bugs) runs here
        props = {"id": self.device_id, "name": self.device_name, "path": "/api"}
        cert = getattr(self.cfg, "tls_cert", None)
        if cert and getattr(self.cfg, "tls_key", None):
            # HTTPS: Home Assistant pins the certificate by this fingerprint (SHA-256 of its DER form)
            from .api import cert_fingerprint

            props["tls"] = "1"
            fingerprint = cert_fingerprint(cert)
            if fingerprint:
                props["fp"] = fingerprint
        return props

    def _addresses_now(self) -> list[str]:
        return list(self._addresses) if self._addresses is not None else lan_addresses(self.cfg.host)

    def _service_info(self, addresses: list[str]) -> Any:
        from zeroconf import ServiceInfo

        host = slugify(socket.gethostname(), sep="-") or "pc"
        return ServiceInfo(
            SERVICE_TYPE,
            f"{slugify(self.device_name, sep='-')}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in addresses],
            port=int(self.cfg.port),
            properties=self.properties(),
            server=f"{host}.local.",
        )

    def start(self) -> bool:
        if not self.cfg.enabled or self.cfg.host in LOOPBACK:
            log.debug("mDNS: API is loopback-only; not advertising")
            return False
        try:
            from zeroconf import IPVersion, Zeroconf
        except ImportError:
            log.info("mDNS advertising skipped: the 'zeroconf' package is not installed (pip install zeroconf)")
            return False
        self.stop()   # started again (the API came back): one announcement, not two
        addresses = self._addresses_now()
        if not addresses:
            log.info("mDNS advertising skipped: no LAN address found")
            return False
        info = self._service_info(addresses)
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        try:
            zc.register_service(info)
        except Exception:
            # e.g. the name is taken on the network: nothing is left half-open, and the
            # announcement counts as not running
            try:
                zc.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        with self._lock:
            self._zc, self._info, self._announced = zc, info, sorted(addresses)
        log.info("mDNS: advertising %s on %s:%s", SERVICE_TYPE, ", ".join(addresses), self.cfg.port)
        if self._addresses is None:
            # addresses change (DHCP, Wi-Fi to cable, a VPN coming up): looked at again every minute
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._refresh_loop, args=(self._stop,), name="mdns-refresh", daemon=True)
            self._thread.start()
        return True

    def _refresh_loop(self, stop: threading.Event) -> None:
        while not stop.wait(REFRESH_SECONDS):
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                log.debug("mDNS address refresh failed", exc_info=True)

    def refresh(self) -> bool:
        """Announce the PC's current addresses if they changed; True when the announcement was updated."""
        addresses = sorted(self._addresses_now())
        with self._lock:
            zc = self._zc
            if zc is None or not addresses or addresses == self._announced:
                return False
            info = self._service_info(addresses)
            zc.update_service(info)
            self._info, self._announced = info, addresses
        log.info("mDNS: addresses changed; now advertising on %s", ", ".join(addresses))
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            zc, self._zc = self._zc, None
        if zc is None:
            return
        try:
            if self._info is not None:
                zc.unregister_service(self._info)
        except Exception:  # noqa: BLE001
            pass
        try:
            zc.close()
        except Exception:  # noqa: BLE001
            pass
