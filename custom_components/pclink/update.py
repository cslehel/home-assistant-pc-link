"""Update entity: the PC Link version installed on the PC vs the latest release.

The PC checks its GitHub releases itself and downloads the update itself; Home
Assistant only shows the versions and, when the PC allows it, starts the install.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    PclinkApiError,
    PclinkAuthError,
    PclinkCommandError,
    PclinkConnectionError,
    PclinkThrottledError,
    PclinkWrongDeviceError,
)
from .entity import PclinkEntity, async_setup_platform_entities

_LOGGER = logging.getLogger(__name__)

# The installer stops the service, so the PC may fall silent the moment it is told to
# update.  For this long after such an install request the update counts as running.
INSTALL_GRACE_SECONDS = 900


class PclinkUpdate(PclinkEntity, UpdateEntity):
    def __init__(self, coordinator, spec: dict[str, Any]) -> None:
        self._install_from: Optional[str] = None   # the version an install was started from
        self._install_until = 0.0
        self._install_gone = False                 # ... and the PC has been unreachable since
        super().__init__(coordinator, spec)

    def _apply_spec(self) -> None:
        super()._apply_spec()
        spec = self.spec
        try:
            self._attr_device_class = UpdateDeviceClass(spec["device_class"]) if spec.get("device_class") else None
        except ValueError:
            self._attr_device_class = None
        self._attr_title = str(spec.get("title") or "PC Link")
        # The PC only offers the command when it can replace itself (standalone build, allowed
        # in its config).  PROGRESS makes Home Assistant read ``in_progress`` from here, i.e.
        # from the PC, for as long as the download and the installation take.
        features = UpdateEntityFeature(0)
        if spec.get("command"):
            features |= UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
        if spec.get("release_notes"):
            features |= UpdateEntityFeature.RELEASE_NOTES
        self._features = features

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Install is not offered when the PC said that our token is not its admin token:
        the request would only be refused.  The progress of an update started on the PC
        itself is still shown."""
        if self.coordinator.admin is False:
            return self._features & ~UpdateEntityFeature.INSTALL
        return self._features

    def _text(self, field: str) -> Optional[str]:
        raw = self.coordinator.value(self.spec, field)
        return None if raw is None else str(raw)

    @property
    def installed_version(self) -> Optional[str]:
        return self._text("value")

    @property
    def latest_version(self) -> Optional[str]:
        return self._text("latest") or self.installed_version

    def _install_pending(self) -> bool:
        """An install was asked for and the PC went quiet over it: still running, as far as
        we can tell, until another version is installed or the grace period is over."""
        if self._install_from is None:
            return False
        if time.monotonic() > self._install_until or self.installed_version != self._install_from:
            self._install_from = None
            return False
        return True

    @property
    def in_progress(self) -> bool:
        return bool(self.coordinator.value(self.spec, "in_progress")) or self._install_pending()

    @property
    def update_percentage(self) -> Optional[float]:
        """Progress in percent when the PC reports one (read by Home Assistant 2024.12 and later)."""
        raw = self.coordinator.value(self.spec, "percentage")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not self.in_progress:
            return None
        return min(max(float(raw), 0.0), 100.0)

    @property
    def release_url(self) -> Optional[str]:
        return self._text("release_url")

    @property
    def release_summary(self) -> Optional[str]:
        text = self._text("summary")
        return text[:255] if text else None

    async def async_release_notes(self) -> Optional[str]:
        return self._text("release_notes")

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._install_pending():
            if not self.coordinator.online:
                self._install_gone = True
            elif self._install_gone and not self.coordinator.value(self.spec, "in_progress"):
                self._install_from = None  # back with the old version and not updating: it did not happen
        super()._handle_coordinator_update()

    async def async_install(self, version: Optional[str], backup: bool, **kwargs: Any) -> None:
        name = self.coordinator.pc_name
        try:
            await self._send("PRESS")
        except PclinkThrottledError as exc:
            raise self.coordinator.throttled_error(exc) from exc
        except PclinkCommandError as exc:
            raise HomeAssistantError(f"{name}: {self._attr_name}: {exc}") from exc
        except PclinkAuthError as exc:
            self.coordinator.async_start_reauth("the API token was rejected for a command")
            raise HomeAssistantError(f"{name} rejected the API token; enter the current one under Settings > Devices & services") from exc
        except PclinkWrongDeviceError as exc:
            raise HomeAssistantError(f"{self.coordinator.entry.title}: {exc}") from exc
        except PclinkConnectionError as exc:
            if not self.coordinator.online:
                raise HomeAssistantError(f"{name} could not be reached: {exc}") from exc
            # The PC was there a moment ago and dropped the connection over this request:
            # that is what a starting installer looks like.  In progress, not failed.
            _LOGGER.info("%s went quiet when asked to update; taking the update as started (%s)", name, exc)
            self._install_from = self.installed_version
            self._install_until = time.monotonic() + INSTALL_GRACE_SECONDS
            self._install_gone = False
        except PclinkApiError as exc:
            raise HomeAssistantError(f"{name} could not be reached: {exc}") from exc
        self.async_write_ha_state()
        await self.coordinator.async_request_state()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_setup_platform_entities(hass, entry, async_add_entities, "update", PclinkUpdate)
