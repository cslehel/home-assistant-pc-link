"""Camera showing the last (parent-requested) screenshot of a user's screen."""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PclinkApiError
from .coordinator import PclinkCoordinator
from .entity import PclinkEntity, async_setup_platform_entities

_LOGGER = logging.getLogger(__name__)


class PclinkCamera(PclinkEntity, Camera):
    def __init__(self, coordinator: PclinkCoordinator, spec: dict[str, Any]) -> None:
        PclinkEntity.__init__(self, coordinator, spec)
        Camera.__init__(self)
        self._image: Optional[bytes] = None
        self._image_stamp: Any = None

    @property
    def meta(self) -> dict[str, Any]:
        raw = self.value
        return raw if isinstance(raw, dict) else {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.meta)

    @property
    def content_type(self) -> str:  # type: ignore[override]
        return "image/png" if self.meta.get("format") == "png" else "image/jpeg"

    @content_type.setter
    def content_type(self, value: str) -> None:  # Camera.__init__ assigns a default
        pass

    @property
    def extra_state_attributes(self) -> Optional[dict[str, Any]]:
        return dict(self.meta) or None

    async def async_camera_image(self, width: Optional[int] = None, height: Optional[int] = None) -> Optional[bytes]:
        stamp = self.meta.get("ts")
        if not stamp:
            return None
        if self._image is not None and stamp == self._image_stamp:
            return self._image
        try:
            data = await self.coordinator.client.screenshot(str(self.spec.get("user", "")))
        except PclinkApiError as exc:
            _LOGGER.debug("screenshot fetch failed: %s", exc)
            return self._image
        if data:
            self._image, self._image_stamp = data, stamp
        return data or self._image


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "camera", PclinkCamera)
