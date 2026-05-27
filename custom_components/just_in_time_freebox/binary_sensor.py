"""Binary sensor for per-target port open/closed state."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FREEBOX_HOST, DOMAIN
from .coordinator import JitFreeboxCoordinator


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    host = entry.data.get(CONF_FREEBOX_HOST, "freebox")
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"JIT Freebox ({host})",
        manufacturer="Just-In-Time Freebox",
        model="Port-forward grant controller",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors from coordinator data."""
    coordinator: JitFreeboxCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Add entities when data first arrives
    async def platform_update_listener() -> None:
        """Handle coordinator data updates."""
        coordinator.async_add_listener(platform_update_listener)

    async def _async_initial_setup() -> None:
        """Initial setup: listen for updates."""
        # Entity creation is now dynamic: entities are created/destroyed
        # as grants appear/disappear in the API response
        coordinator.async_add_listener(_update_entities)

    async def _update_entities() -> None:
        """Update or create entities based on coordinator data."""
        if not coordinator.data:
            return

        active_grants = coordinator.data.get("active_grants", [])
        current_entities = {
            entity.unique_id: entity
            for entity in hass.data.get(f"{DOMAIN}_{entry.entry_id}_entities", [])
        }

        # Track which targetIds are in the current grants
        current_target_ids = {grant.get("targetId") for grant in active_grants}

        # Remove entities for targets no longer in the API
        to_remove = []
        for entity_id, entity in current_entities.items():
            target_id = entity._target_id  # noqa: SLF001
            if target_id not in current_target_ids:
                to_remove.append(entity)

        for entity in to_remove:
            await hass.config_entries.async_forward_entry_unload(
                entry, "binary_sensor"
            )

        # Create or update entities for current grants
        to_add = []
        for grant in active_grants:
            target_id = grant.get("targetId", "unknown")
            port = grant.get("port")
            unique_id = f"{entry.entry_id}_{target_id}_opened"

            if unique_id not in current_entities:
                to_add.append(
                    JitFreeboxTargetBinarySensor(
                        coordinator, entry, target_id, port
                    )
                )

        if to_add:
            async_add_entities(to_add)
            if f"{DOMAIN}_{entry.entry_id}_entities" not in hass.data:
                hass.data[f"{DOMAIN}_{entry.entry_id}_entities"] = []
            hass.data[f"{DOMAIN}_{entry.entry_id}_entities"].extend(to_add)

    # Schedule initial setup after first coordinator update
    await _async_initial_setup()
    coordinator.async_add_listener(_update_entities)


class JitFreeboxTargetBinarySensor(
    CoordinatorEntity[JitFreeboxCoordinator], BinarySensorEntity
):
    """Binary sensor for a single target's open/closed status."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_translation_key = "target_opened"

    def __init__(
        self,
        coordinator: JitFreeboxCoordinator,
        entry: ConfigEntry,
        target_id: str,
        port: int | None,
    ) -> None:
        super().__init__(coordinator)
        self._target_id = target_id
        self._port = port

        # Entity name: "OpenVPN (1194) Opened"
        self._attr_name = f"{target_id.replace('_', ' ').title()} ({port})" if port else target_id

        self._attr_unique_id = f"{entry.entry_id}_{target_id}_opened"
        self._attr_device_info = _device_info(entry)

    def _get_grant(self) -> dict[str, Any] | None:
        """Find the grant for this target."""
        data = self.coordinator.data or {}
        active_grants = data.get("active_grants", [])
        for grant in active_grants:
            if grant.get("targetId") == self._target_id:
                return grant
        return None

    @property
    def is_on(self) -> bool | None:
        """Return True if the port is open (granted and not expired)."""
        grant = self._get_grant()
        if grant is None:
            return None

        granted = bool(grant.get("granted", False))
        expires_utc = grant.get("expires_utc")

        # Check if expired
        if expires_utc is not None:
            try:
                from datetime import datetime, timezone

                expires_dt = datetime.fromisoformat(expires_utc)
                now = datetime.now(timezone.utc)
                not_expired = expires_dt > now
            except (ValueError, TypeError):
                not_expired = False
        else:
            not_expired = True

        return granted and not_expired

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for this target."""
        grant = self._get_grant()
        if not grant:
            return {}

        return {
            "targetId": grant.get("targetId"),
            "port": grant.get("port"),
            "protocol": grant.get("protocol"),
            "remaining_seconds": grant.get("remaining_seconds"),
            "expires_at": grant.get("expires_utc"),
        }
