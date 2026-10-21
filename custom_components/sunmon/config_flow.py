"""Config flow: the dialog shown under Settings > Devices & services."""

from __future__ import annotations

from typing import Any

# Home Assistant 2026.9+ validates with "probatio" instead of "voluptuous".
import probatio as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import (
    MAX_CHANNELS,
    SolarCountClient,
    SolarCountConnectionError,
    SolarCountError,
    list_serial_ports,
    set_trace,
)
from .const import (
    CONF_CHANNELS,
    CONF_READ_YIELDS,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL_PORT,
    CONF_TRACE_SERIAL,
    CONF_YIELD_QUERY_PAUSE,
    DEFAULT_CHANNELS,
    DEFAULT_READ_YIELDS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SERIAL_PORT,
    DEFAULT_TITLE,
    DEFAULT_YIELD_QUERY_PAUSE,
    DOMAIN,
    LOGGER,
    MAX_SCAN_INTERVAL,
    MAX_YIELD_QUERY_PAUSE,
    MIN_SCAN_INTERVAL,
)

_CHANNELS_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1, max=MAX_CHANNELS, step=1, mode=selector.NumberSelectorMode.BOX
    )
)
_SCAN_INTERVAL_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL,
        max=MAX_SCAN_INTERVAL,
        step=1,
        unit_of_measurement="s",
        mode=selector.NumberSelectorMode.BOX,
    )
)

_YIELD_QUERY_PAUSE_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=MAX_YIELD_QUERY_PAUSE,
        step=10,
        unit_of_measurement="ms",
        mode=selector.NumberSelectorMode.BOX,
    )
)


def _port_selector(ports: list[str]) -> selector.Selector:
    """Offer the detected serial ports, but allow typing any other path."""
    if not ports:
        return selector.TextSelector()
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=ports,
            custom_value=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class SunMonConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    def __init__(self) -> None:
        """Start without settings from an earlier attempt."""
        self._pending: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the serial port, the number of channels and the interval."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                CONF_SERIAL_PORT: user_input[CONF_SERIAL_PORT].strip(),
                CONF_CHANNELS: int(user_input[CONF_CHANNELS]),
                CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                CONF_READ_YIELDS: bool(
                    user_input.get(CONF_READ_YIELDS, DEFAULT_READ_YIELDS)
                ),
                CONF_TRACE_SERIAL: bool(user_input.get(CONF_TRACE_SERIAL, False)),
            }
            self._async_abort_entries_match({CONF_SERIAL_PORT: data[CONF_SERIAL_PORT]})

            # Trace the connection test too: that is when it is needed most.
            set_trace("setup", data[CONF_TRACE_SERIAL])
            try:
                await self.hass.async_add_executor_job(
                    SolarCountClient(data[CONF_SERIAL_PORT], data[CONF_CHANNELS]).probe
                )
            except SolarCountConnectionError as err:
                LOGGER.warning("Serial port problem: %s", err)
                errors["base"] = "cannot_connect"
            except SolarCountError as err:
                LOGGER.warning("SolarCount problem: %s", err)
                self._pending = data
                return await self.async_step_no_response()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected error while setting up")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=DEFAULT_TITLE, data=data)
            finally:
                set_trace("setup", False)

        user_input = user_input or self._pending or None
        ports = await self.hass.async_add_executor_job(list_serial_ports)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SERIAL_PORT, default=ports[0] if ports else DEFAULT_SERIAL_PORT
                ): _port_selector(ports),
                vol.Required(
                    CONF_CHANNELS, default=DEFAULT_CHANNELS
                ): _CHANNELS_SELECTOR,
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): _SCAN_INTERVAL_SELECTOR,
                vol.Required(
                    CONF_READ_YIELDS, default=DEFAULT_READ_YIELDS
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_TRACE_SERIAL, default=False
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_no_response(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The port opened but the SolarCount did not answer: ask what to do."""
        return self.async_show_menu(
            step_id="no_response",
            menu_options=["retry", "user", "setup_anyway"],
            description_placeholders={"port": self._pending[CONF_SERIAL_PORT]},
        )

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run the connection test again with the same settings."""
        return await self.async_step_user(self._pending)

    async def async_step_setup_anyway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry without an answer; setup retries until one comes."""
        return self.async_create_entry(title=DEFAULT_TITLE, data=self._pending)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow (the "Configure" button)."""
        return SunMonOptionsFlow()


class SunMonOptionsFlow(OptionsFlow):
    """Change channels, interval, yield reading and traffic logging after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_CHANNELS: int(user_input[CONF_CHANNELS]),
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_READ_YIELDS: bool(user_input[CONF_READ_YIELDS]),
                    CONF_YIELD_QUERY_PAUSE: int(user_input[CONF_YIELD_QUERY_PAUSE]),
                    CONF_TRACE_SERIAL: bool(user_input[CONF_TRACE_SERIAL]),
                }
            )

        entry = self.config_entry
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CHANNELS,
                    default=entry.options.get(
                        CONF_CHANNELS, entry.data.get(CONF_CHANNELS, DEFAULT_CHANNELS)
                    ),
                ): _CHANNELS_SELECTOR,
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=entry.options.get(
                        CONF_SCAN_INTERVAL,
                        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ),
                ): _SCAN_INTERVAL_SELECTOR,
                vol.Required(
                    CONF_READ_YIELDS,
                    default=entry.options.get(
                        CONF_READ_YIELDS,
                        entry.data.get(CONF_READ_YIELDS, DEFAULT_READ_YIELDS),
                    ),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_YIELD_QUERY_PAUSE,
                    default=entry.options.get(
                        CONF_YIELD_QUERY_PAUSE, DEFAULT_YIELD_QUERY_PAUSE
                    ),
                ): _YIELD_QUERY_PAUSE_SELECTOR,
                vol.Required(
                    CONF_TRACE_SERIAL,
                    default=entry.options.get(
                        CONF_TRACE_SERIAL, entry.data.get(CONF_TRACE_SERIAL, False)
                    ),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
