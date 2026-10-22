"""Sensor platform: channel values, their sums, the energy, and the RS485 counters."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfInformation,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ChannelReading, ChannelYield, SolarCountData, SolarCountStats
from .coordinator import SunMonConfigEntry, SunMonCoordinator
from .entity import SunMonEntity, device_info


@dataclass(frozen=True, kw_only=True)
class SunMonSensorDescription(SensorEntityDescription):
    """Describes a sensor and how to read its value from a channel reading."""

    value_fn: Callable[[ChannelReading], int]


SENSORS: tuple[SunMonSensorDescription, ...] = (
    SunMonSensorDescription(
        key="power",
        translation_key="channel_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda reading: reading.power,
    ),
    SunMonSensorDescription(
        key="voltage",
        translation_key="channel_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda reading: reading.voltage,
    ),
)


@dataclass(frozen=True, kw_only=True)
class SunMonYieldDescription(SensorEntityDescription):
    """Describes a yield sensor and how to read it from a channel's yields."""

    value_fn: Callable[[ChannelYield], int | None]


# The SolarCount's unit for these numbers is not known, so they are shown as
# the raw values it reports, without a unit.
YIELDS: tuple[SunMonYieldDescription, ...] = (
    SunMonYieldDescription(
        key="yield_previous_day",
        translation_key="channel_yield_previous_day",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda yields: yields.previous_day,
    ),
    SunMonYieldDescription(
        key="yield_previous_hour",
        translation_key="channel_yield_previous_hour",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda yields: yields.previous_hour,
    ),
    SunMonYieldDescription(
        key="yield_previous_month",
        translation_key="channel_yield_previous_month",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda yields: yields.previous_month,
    ),
    SunMonYieldDescription(
        key="yield_previous_year",
        translation_key="channel_yield_previous_year",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda yields: yields.previous_year,
    ),
)


def _sum(values: Iterable[int | None]) -> int | None:
    """Add up the values of all channels.

    The sum is unknown if any channel is: a sum over only some of the channels
    would look like a real drop.
    """
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


@dataclass(frozen=True, kw_only=True)
class SunMonSumDescription(SensorEntityDescription):
    """Describes a sensor that adds one value up over all channels."""

    value_fn: Callable[[SolarCountData], int | None]


SUMS: tuple[SunMonSumDescription, ...] = (
    SunMonSumDescription(
        key="sum_power",
        translation_key="sum_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.total_power,
    ),
)

YIELD_SUMS: tuple[SunMonSumDescription, ...] = (
    SunMonSumDescription(
        key="sum_yield_previous_day",
        translation_key="sum_yield_previous_day",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _sum(yields.previous_day for yields in data.yields),
    ),
    SunMonSumDescription(
        key="sum_yield_previous_hour",
        translation_key="sum_yield_previous_hour",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _sum(yields.previous_hour for yields in data.yields),
    ),
    SunMonSumDescription(
        key="sum_yield_previous_month",
        translation_key="sum_yield_previous_month",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _sum(yields.previous_month for yields in data.yields),
    ),
    SunMonSumDescription(
        key="sum_yield_previous_year",
        translation_key="sum_yield_previous_year",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _sum(yields.previous_year for yields in data.yields),
    ),
)


@dataclass(frozen=True, kw_only=True)
class SunMonCounterDescription(SensorEntityDescription):
    """Describes an RS485 counter and how to read it from the statistics."""

    value_fn: Callable[[SolarCountStats], float | None]


COUNTERS: tuple[SunMonCounterDescription, ...] = (
    SunMonCounterDescription(
        key="rs485_requests",
        translation_key="rs485_requests",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda stats: stats.requests,
    ),
    SunMonCounterDescription(
        key="rs485_read_ok",
        translation_key="rs485_read_ok",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda stats: stats.read_ok,
    ),
    SunMonCounterDescription(
        key="rs485_crc_errors",
        translation_key="rs485_crc_errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda stats: stats.crc_errors,
    ),
    SunMonCounterDescription(
        key="rs485_no_response",
        translation_key="rs485_no_response",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda stats: stats.no_response,
    ),
    SunMonCounterDescription(
        key="rs485_unexpected",
        translation_key="rs485_unexpected",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda stats: stats.unexpected,
    ),
    SunMonCounterDescription(
        key="rs485_send_rate",
        translation_key="rs485_send_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda stats: stats.send_rate,
    ),
    SunMonCounterDescription(
        key="rs485_receive_rate",
        translation_key="rs485_receive_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda stats: stats.receive_rate,
    ),
    SunMonCounterDescription(
        key="rs485_bytes_sent",
        translation_key="rs485_bytes_sent",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda stats: stats.bytes_sent,
    ),
    SunMonCounterDescription(
        key="rs485_bytes_received",
        translation_key="rs485_bytes_received",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda stats: stats.bytes_received,
    ),
    SunMonCounterDescription(
        key="rs485_bus_load",
        translation_key="rs485_bus_load",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda stats: stats.bus_load,
    ),
    SunMonCounterDescription(
        key="rs485_error_rate",
        translation_key="rs485_error_rate",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda stats: stats.error_rate,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunMonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors of every configured channel, the sums and the counters."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        SunMonSensor(coordinator, channel, description)
        for channel in range(coordinator.client.channels)
        for description in SENSORS
    ]
    entities.extend(SunMonSumSensor(coordinator, description) for description in SUMS)
    entities.append(SunMonEnergySensor(coordinator))
    entities.extend(
        SunMonYieldSensor(coordinator, channel, description)
        for channel in range(coordinator.client.channels)
        for description in YIELDS
    )
    entities.extend(
        SunMonSumSensor(coordinator, description) for description in YIELD_SUMS
    )
    entities.extend(SunMonCounter(coordinator, description) for description in COUNTERS)
    async_add_entities(entities)


class SunMonSensor(SunMonEntity, SensorEntity):
    """One measured value of one SolarCount channel."""

    entity_description: SunMonSensorDescription

    def __init__(
        self,
        coordinator: SunMonCoordinator,
        channel: int,
        description: SunMonSensorDescription,
    ) -> None:
        """Initialise the sensor. ``channel`` is the SolarCount index 0 to 5."""
        super().__init__(coordinator, f"ch{channel}_{description.key}")
        self.entity_description = description
        self._channel = channel
        # Shown as channel 1 to 6, like P1, P2, P3 in the firmware.
        self._attr_translation_placeholders = {"channel": str(channel + 1)}

    @property
    def native_value(self) -> int | None:
        """Return the value, or None if this channel's response was invalid."""
        channels = self.coordinator.data.channels
        if self._channel >= len(channels):
            return None
        reading = channels[self._channel]
        if reading is None:
            return None
        return self.entity_description.value_fn(reading)


class SunMonYieldSensor(SunMonEntity, SensorEntity):
    """One yield value of one SolarCount channel."""

    entity_description: SunMonYieldDescription

    def __init__(
        self,
        coordinator: SunMonCoordinator,
        channel: int,
        description: SunMonYieldDescription,
    ) -> None:
        """Initialise the sensor. ``channel`` is the SolarCount index 0 to 5."""
        super().__init__(coordinator, f"ch{channel}_{description.key}")
        self.entity_description = description
        self._channel = channel
        self._attr_translation_placeholders = {"channel": str(channel + 1)}

    @property
    def native_value(self) -> int | None:
        """Return the value, or None if it could not be read."""
        yields = self.coordinator.data.yields
        if self._channel >= len(yields):
            return None
        return self.entity_description.value_fn(yields[self._channel])


class SunMonSumSensor(SunMonEntity, SensorEntity):
    """The sum of one value over all configured channels."""

    entity_description: SunMonSumDescription

    def __init__(
        self,
        coordinator: SunMonCoordinator,
        description: SunMonSumDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        """Return the sum, or None if a channel's value is unknown."""
        return self.entity_description.value_fn(self.coordinator.data)


class SunMonEnergySensor(SunMonEntity, RestoreSensor):
    """The total energy generated, from the total power of every cycle.

    The total is kept across restarts of Home Assistant.
    """

    _attr_translation_key = "total_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: SunMonCoordinator) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, "total_energy")

    async def async_added_to_hass(self) -> None:
        """Continue from the total before the last restart or reload."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float | Decimal):
            self.coordinator.energy.total_kwh += float(last.native_value)

    @property
    def native_value(self) -> float:
        """Return the total in kWh."""
        return round(self.coordinator.energy.total_kwh, 6)


class SunMonCounter(SensorEntity):
    """A counter of the RS485 traffic.

    Not tied to the success of the read cycle: it stays available and keeps
    counting while the SolarCount does not answer.
    """

    entity_description: SunMonCounterDescription

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SunMonCoordinator,
        description: SunMonCounterDescription,
    ) -> None:
        """Initialise the counter."""
        self.entity_description = description
        self._coordinator = coordinator
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Update after every read cycle, successful or not."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_stats_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> float | None:
        """Return the current value, or None if it is not known yet."""
        return self.entity_description.value_fn(self._coordinator.client.stats)
