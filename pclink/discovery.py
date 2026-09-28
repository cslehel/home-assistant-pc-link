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
from typing import Any, Optional

from . import __version__
from .config import ApiConfig
from .util import slugify

log = logging.getLogger(__name__)

SERVICE_TYPE = "_pclink._tcp.local."
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def lan_addresses(bind_host: str) -> list[str]:
    """IPv4 addresses to announce for a given bind address."""
    if bind_host and bind_host not in ("0.0.0.0", "::", "") and bind_host not in LOOPBACK:
        return [bind_host]
    found: list[str] = []
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and a.address and not a.address.startswith(("127.", "169.254.")):
                    found.append(a.address)
    except Exception:  # noqa: BLE001
        pass
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

    @property
    def active(self) -> bool:
        return self._zc is not None

    def properties(self) -> dict[str, str]:
        return {"id": self.device_id, "name": self.device_name, "version": __version__, "path": "/api"}

    def start(self) -> bool:
        if not self.cfg.enabled or self.cfg.host in LOOPBACK:
            log.debug("mDNS: API is loopback-only; not advertising")
            return False
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError:
            log.info("mDNS advertising skipped: the 'zeroconf' package is not installed (pip install zeroconf)")
            return False
        addresses = self._addresses if self._addresses is not None else lan_addresses(self.cfg.host)
        if not addresses:
            log.info("mDNS advertising skipped: no LAN address found")
            return False
        host = slugify(socket.gethostname(), sep="-") or "pc"
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{slugify(self.device_name, sep='-')}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in addresses],
            port=int(self.cfg.port),
            properties=self.properties(),
            server=f"{host}.local.",
        )
        self._zc = Zeroconf(ip_version=IPVersion.V4Only)
        self._zc.register_service(self._info)
        log.info("mDNS: advertising %s on %s:%s", SERVICE_TYPE, ", ".join(addresses), self.cfg.port)
        return True

    def stop(self) -> None:
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
