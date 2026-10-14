"""Buttons (power, +15/+30 minutes, rewards, log off, lock, screenshot, launch/close apps, custom commands)."""
from __future__ import annotations

import socket
from typing import Any, Optional

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PclinkCoordinator
from .entity import PclinkEntity, async_setup_platform_entities


class PclinkButton(PclinkEntity, ButtonEntity):
    def _apply_spec(self) -> None:
        super()._apply_spec()
        spec = self.spec
        try:
            self._attr_device_class = ButtonDeviceClass(spec["device_class"]) if spec.get("device_class") else None
        except ValueError:
            self._attr_device_class = None

    @property
    def available(self) -> bool:
        # buttons have no state; the PC has to be reachable and still offer the button
        return self.coordinator.online and self.in_catalog

    async def async_press(self) -> None:
        await self.async_send("PRESS")


def magic_packet(mac: str) -> bytes:
    """The Wake-on-LAN payload for a MAC address written with ``:``, ``-`` or no separators."""
    digits = "".join(ch for ch in mac if ch not in ":-. ")
    if len(digits) != 12:
        raise ValueError(f"invalid MAC address {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(digits) * 16


def _ipv4(text: Optional[str]) -> Optional[str]:
    parts = (text or "").strip().split(".")
    if len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts):
        return ".".join(parts)
    return None


def wake_targets(ip: Optional[str], broadcast: Optional[str] = None) -> list[str]:
    """Where to send the packet: the LAN broadcast, the directed broadcast of the PC's
    network (this one also passes routers and containers) and the PC's last address.

    The PC reports its network's broadcast address; for one that does not (an older
    service) the usual /24 home network is assumed."""
    targets = ["255.255.255.255"]
    ip, broadcast = _ipv4(ip), _ipv4(broadcast)
    if broadcast is None and ip is not None:
        broadcast = ".".join(ip.split(".")[:3] + ["255"])
    for target in (broadcast, ip):
        if target is not None and target not in targets:
            targets.append(target)
    return targets


def send_magic_packet(mac: str, ip: Optional[str], broadcast: Optional[str] = None) -> int:
    """Send the packet to every target; returns how many sends succeeded (blocking: run in the executor)."""
    packet = magic_packet(mac)
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in wake_targets(ip, broadcast):
            for port in (9, 7):
                try:
                    sock.sendto(packet, (target, port))
                    sent += 1
                except OSError:
                    continue
    return sent


class PclinkWakeButton(CoordinatorEntity[PclinkCoordinator], ButtonEntity):
    """Wake-on-LAN: sent by Home Assistant itself, because the PC is off when it is needed."""

    _attr_has_entity_name = True
    _attr_name = "Wake up"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: PclinkCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.wake_unique_id
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return bool(self.coordinator.wake_mac)  # usable exactly when the PC is not reachable

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"mac_address": self.coordinator.wake_mac, "broadcast_address": self.coordinator.wake_broadcast}

    async def async_press(self) -> None:
        mac = self.coordinator.wake_mac
        if not mac:
            raise HomeAssistantError(f"{self.coordinator.pc_name}: the MAC address is not known yet")
        try:
            sent = await self.hass.async_add_executor_job(
                send_magic_packet, mac, self.coordinator.wake_ip, self.coordinator.wake_broadcast)
        except (OSError, ValueError, TypeError) as exc:
            raise HomeAssistantError(f"{self.coordinator.pc_name}: could not send the wake-up packet: {exc}") from exc
        if not sent:
            raise HomeAssistantError(f"{self.coordinator.pc_name}: the network refused the wake-up packet")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "button", PclinkButton)
    async_add_entities([PclinkWakeButton(hass.data[DOMAIN][entry.entry_id])])
