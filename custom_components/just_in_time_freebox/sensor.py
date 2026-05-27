"""Sensor entities for Just-In-Time Freebox."""
from __future__ import annotations

import json
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ACTION_DISABLED,
    ACTION_ENABLED,
    ACTION_FREEBOX_ERROR,
    ACTION_IDLE,
    ACTION_PARTIAL_SUCCESS,
    ACTION_RULE_NOT_FOUND,
    CONF_FREEBOX_HOST,
    DOMAIN,
)
from .coordinator import JitFreeboxCoordinator


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    host = entry.data.get(CONF_FREEBOX_HOST, "freebox")
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"JIT Freebox ({host})",
        manufacturer="Just-In-Time Freebox",
        model="Port-forward grant controller",
    )


SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="grants_active_count",
        translation_key="grants_active_count",
        name="Grants Active Count",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="targets_summary",
        translation_key="targets_summary",
        name="Targets Summary",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="last_action",
        translation_key="last_action",
        name="Last Action",
        device_class=SensorDeviceClass.ENUM,
        options=[
            ACTION_IDLE,
            ACTION_ENABLED,
            ACTION_DISABLED,
            ACTION_RULE_NOT_FOUND,
            ACTION_FREEBOX_ERROR,
            ACTION_PARTIAL_SUCCESS,
        ],
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: JitFreeboxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        JitFreeboxSensor(coordinator, entry, desc) for desc in SENSORS
    )


class JitFreeboxSensor(CoordinatorEntity[JitFreeboxCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JitFreeboxCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        key = self.entity_description.key

        if key == "grants_active_count":
            active_grants = data.get("active_grants", [])
            granted_count = sum(
                1 for g in active_grants
                if g.get("granted") and g.get("expires_utc")
            )
            return granted_count

        if key == "targets_summary":
            active_grants = data.get("active_grants", [])
            granted_count = sum(
                1 for g in active_grants if g.get("granted")
            )
            denied_count = sum(
                1 for g in active_grants if not g.get("granted")
            )
            return f"{granted_count} granted, {denied_count} denied"

        if key == "last_action":
            return data.get("last_action", ACTION_IDLE)

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        key = self.entity_description.key

        if key == "targets_summary":
            active_grants = data.get("active_grants", [])
            return {
                "targets": json.dumps(active_grants, default=str),
                "total_count": len(active_grants),
            }

        if key == "last_action":
            return {
                "actions_taken": data.get("actions_taken", []),
                "failed_targets": data.get("failed_targets", []),
                "timestamp": data.get("timestamp"),
            }

        return {}
