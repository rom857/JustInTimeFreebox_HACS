"""Just-In-Time Freebox integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_POLL_INTERVAL, DOMAIN, PLATFORMS
from .coordinator import JitFreeboxCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = JitFreeboxCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options updates."""
    coordinator: JitFreeboxCoordinator = hass.data[DOMAIN][entry.entry_id]
    new_poll = entry.options.get(
        CONF_POLL_INTERVAL, entry.data.get(CONF_POLL_INTERVAL)
    )
    if new_poll is not None:
        coordinator.update_poll_interval(int(new_poll))
    # Trigger an immediate refresh so URL/key changes apply now.
    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
