"""Data update coordinator: runs one SolarCount cycle per interval."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HELD_READS,
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
        self._problem_channels: tuple[int, ...] = ()
        # Integrates the total power of every completed cycle into kWh.
        self.energy = EnergyIntegrator()

    @property
    def problem_channels(self) -> tuple[int, ...]:
        """Channels with a value that is unavailable after repeated faults (0 to 5)."""
        return self._hold.problem_channels

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

        A value that cannot be read keeps its last good value for up to
        HELD_READS failed reads in a row, then it is unavailable. A failed
        cycle only fails the update before the first good one, so that the
        indicator of the faulty values stays available.
        """
        try:
            data = await self.hass.async_add_executor_job(self.client.read)
        except SolarCountError as err:
            self.energy.interrupt()
            self._async_notify_stats_listeners()
            if self.data is None:
                raise UpdateFailed(str(err)) from err
            LOGGER.debug("Reading failed: %s", err)
            data = self._hold.after_failure(self.client.yields, self.client.yield_round)
        else:
            self._async_notify_stats_listeners()
            data = self._hold.apply(data)
            self.energy.add(time.monotonic(), data.total_power)
        self._log_problems()
        return data

    def _log_problems(self) -> None:
        """Log when channels become unavailable, and when they are back."""
        problems = self._hold.problem_channels
        if problems == self._problem_channels:
            return
        new = sorted(set(problems) - set(self._problem_channels))
        if new:
            LOGGER.warning(
                "Channel %s could not be read %d times in a row, its values are"
                " unavailable until it can be read again",
                ", ".join(str(channel + 1) for channel in new),
                HELD_READS + 1,
            )
        if not problems:
            LOGGER.info("All values can be read again")
        self._problem_channels = problems

    @callback
    def _async_notify_stats_listeners(self) -> None:
        for listener in list(self._stats_listeners):
            listener()
