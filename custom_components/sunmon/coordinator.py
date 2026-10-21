"""Data update coordinator: runs one SolarCount cycle per interval."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HOLD_TIME,
    EnergyIntegrator,
    SolarCountClient,
    SolarCountData,
    SolarCountError,
    ValueHold,
)
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, LOGGER

type SunMonConfigEntry = ConfigEntry[SunMonCoordinator]


class SunMonCoordinator(DataUpdateCoordinator[SolarCountData]):
    """Fetches the channel values from the SolarCount on a fixed interval."""

    config_entry: SunMonConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SunMonConfigEntry,
        client: SolarCountClient,
    ) -> None:
        """Initialise the coordinator."""
        interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self._stats_listeners: set[Callable[[], None]] = set()
        self._hold = ValueHold()
        self._holding = False
        # Integrates the total power of every completed cycle into kWh.
        self.energy = EnergyIntegrator()

    @property
    def held_channels(self) -> tuple[int, ...]:
        """Channels whose current values are held after a fault (0 to 5)."""
        return self._hold.held_channels

    @callback
    def async_add_stats_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Call ``listener`` after every cycle, also after a failed one.

        The normal coordinator listeners are not called while the SolarCount
        stays unreachable, but the RS485 counters keep changing then.
        """
        self._stats_listeners.add(listener)

        @callback
        def remove() -> None:
            self._stats_listeners.discard(listener)

        return remove

    async def async_reset_stats(self) -> None:
        """Set the RS485 counters and throughput back to zero."""
        await self.hass.async_add_executor_job(self.client.reset_stats)
        self._async_notify_stats_listeners()

    async def _async_update_data(self) -> SolarCountData:
        """Read all channels. The serial I/O blocks, so it runs in a thread.

        After a fault, the last good values are kept for up to a minute.
        """
        try:
            data = await self.hass.async_add_executor_job(self.client.read)
        except SolarCountError as err:
            self.energy.interrupt()
            self._async_notify_stats_listeners()
            held = self._hold.after_failure()
            if held is None:
                self._holding = False
                raise UpdateFailed(str(err)) from err
            if not self._holding:
                self._holding = True
                LOGGER.warning(
                    "Reading failed, keeping the last values for up to %d seconds: %s",
                    HOLD_TIME,
                    err,
                )
            return held
        if self._holding:
            self._holding = False
            LOGGER.info("Reading works again")
        self._async_notify_stats_listeners()
        data = self._hold.apply(data)
        self.energy.add(time.monotonic(), data.total_power)
        return data

    @callback
    def _async_notify_stats_listeners(self) -> None:
        for listener in list(self._stats_listeners):
            listener()
