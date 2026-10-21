"""SunMon: reads a SolarCount over RS485 into Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import SolarCountClient, set_trace
from .const import (
    CONF_CHANNELS,
    CONF_SERIAL_PORT,
    CONF_TRACE_SERIAL,
    DEFAULT_CHANNELS,
)
from .coordinator import SunMonConfigEntry, SunMonCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SunMonConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    channels = entry.options.get(
        CONF_CHANNELS, entry.data.get(CONF_CHANNELS, DEFAULT_CHANNELS)
    )
    trace = entry.options.get(
        CONF_TRACE_SERIAL, entry.data.get(CONF_TRACE_SERIAL, False)
    )
    set_trace(entry.entry_id, bool(trace))

    client = SolarCountClient(
        entry.data[CONF_SERIAL_PORT], int(channels), read_yields=True
    )
    coordinator = SunMonCoordinator(hass, entry, client)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Free the serial port so the automatic retry can open it again.
        await hass.async_add_executor_job(client.close)
        raise

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunMonConfigEntry) -> bool:
    """Unload a config entry and release the serial port."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await hass.async_add_executor_job(entry.runtime_data.client.close)
        set_trace(entry.entry_id, False)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: SunMonConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
