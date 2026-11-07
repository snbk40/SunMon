"""Binary sensor platform: shows when values are unavailable after repeated faults."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SunMonConfigEntry, SunMonCoordinator
from .entity import SunMonEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunMonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the indicator for a config entry."""
    async_add_entities([SunMonValuesStatus(entry.runtime_data)])


class SunMonValuesStatus(SunMonEntity, BinarySensorEntity):
    """On while a value is unavailable because it failed too often in a row.

    Up to HELD_READS failed reads are bridged with the last good value and
    leave this off.
    """

    _attr_translation_key = "values_status"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SunMonCoordinator) -> None:
        """Initialise the indicator."""
        # Keeps the ID from when it was called "Values held", so existing
        # installations keep the same entity and its history.
        super().__init__(coordinator, "values_held")

    @property
    def is_on(self) -> bool:
        """Return true if any channel has a value that is unavailable."""
        return bool(self.coordinator.problem_channels)

    @property
    def extra_state_attributes(self) -> dict[str, list[int]]:
        """Return the affected channels, numbered 1 to 6 as in the names."""
        return {
            "channels": [channel + 1 for channel in self.coordinator.problem_channels]
        }
