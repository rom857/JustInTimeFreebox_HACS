"""Binary sensor for the granted state."""
from __future__ import annotations

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
    coordinator: JitFreeboxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([JitFreeboxGrantedBinarySensor(coordinator, entry)])


class JitFreeboxGrantedBinarySensor(
    CoordinatorEntity[JitFreeboxCoordinator], BinarySensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "Granted"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "granted"

    def __init__(
        self, coordinator: JitFreeboxCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_granted"
        self._attr_device_info = _device_info(entry)

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        return data.get("granted")
