"""Button platform: sets the RS485 diagnostics back to zero."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SunMonConfigEntry, SunMonCoordinator
from .entity import device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunMonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the reset button for a config entry."""
    async_add_entities([SunMonResetDiagnostics(entry.runtime_data)])


class SunMonResetDiagnostics(ButtonEntity):
    """Sets the RS485 counters, byte counts, throughput and bus load to 0.

    Not tied to the success of the read cycle, like the counters it resets.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reset_diagnostics"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SunMonCoordinator) -> None:
        """Initialise the button."""
        self._coordinator = coordinator
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_reset_diagnostics"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        """Set the diagnostics to 0."""
        await self._coordinator.async_reset_stats()
