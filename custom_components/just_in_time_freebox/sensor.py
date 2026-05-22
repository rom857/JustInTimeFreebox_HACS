"""Sensor entities for Just-In-Time Freebox."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ACTION_DISABLED,
    ACTION_ENABLED,
    ACTION_FREEBOX_ERROR,
    ACTION_IDLE,
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
        key="status",
        translation_key="status",
        name="Status",
        device_class=SensorDeviceClass.ENUM,
        options=["granted", "not_granted", "unknown"],
    ),
    SensorEntityDescription(
        key="port",
        translation_key="port",
        name="Port",
    ),
    SensorEntityDescription(
        key="protocol",
        translation_key="protocol",
        name="Protocol",
        device_class=SensorDeviceClass.ENUM,
        options=["tcp", "udp"],
    ),
    SensorEntityDescription(
        key="expires_at",
        translation_key="expires_at",
        name="Expires at",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="remaining_seconds",
        translation_key="remaining_seconds",
        name="Remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    SensorEntityDescription(
        key="last_action",
        translation_key="last_action",
        name="Last action",
        device_class=SensorDeviceClass.ENUM,
        options=[
            ACTION_IDLE,
            ACTION_ENABLED,
            ACTION_DISABLED,
            ACTION_RULE_NOT_FOUND,
            ACTION_FREEBOX_ERROR,
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
        if key == "status":
            granted = data.get("granted")
            if granted is True:
                return "granted"
            if granted is False:
                return "not_granted"
            return "unknown"
        if key == "port":
            return data.get("port")
        if key == "protocol":
            return data.get("protocol")
        if key == "expires_at":
            return data.get("expires_utc")
        if key == "remaining_seconds":
            return data.get("remaining_seconds")
        if key == "last_action":
            return data.get("last_action") or ACTION_IDLE
        return None
