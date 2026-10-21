"""SolarCount RS485 client.

Speaks the protocol used by the SolarCount task of the CP-JR ARM7 LPC2368
firmware (Source/EnergyMeters/SolarCountUART.c):

* 57600 baud, 8 data bits, no parity, 1 stop bit
* frame:  STX(02) destination source length(2, little endian) command data
          CRC16(2, little endian) ETX(03)
* the host is address F0, the SolarCount is address B0
* ``length`` counts command + data + CRC
* the CRC is CRC-16/CCITT (start value FFFF) over destination..data
* a response carries the request's command with bit 7 set

One measuring cycle, as in the firmware: send command 21 ("init"), then ask
for each channel with command 20. The channel response holds the power in
watts (16 bit, little endian, read as signed) and the voltage in volts (8 bit).

Once a minute the firmware also reads the yields. It asks for the SolarCount's
clock with command 04, then for the yield of the previous day and of the
previous hour of each channel. This client also asks for the yield of the
previous month and of the previous year, which the firmware does not; a
captured month exchange in the firmware source shows the frames, and its notes
list 'Y' as the year period. The yield queries use a second, text style frame:

* request:   STX '1' '<' period '0' channel '0' '0' value(2 digits) CRC8
             ETX, where period is 'D' (day), 'h' (hour), 'M' (month) or 'Y'
             (year) and value is the day of the month or the hour as hex
             digits, or the month or the last two digits of the year as
             decimal (BCD) digits
* response:  STX '1' period, the six request characters, the yield as eight
             hex digits, CRC8, ETX, ETX
* the CRC8 covers everything between STX and the CRC and always has bit 7 set

This module has no Home Assistant imports and does blocking I/O with the
Python standard library only, so call it from an executor thread.
"""

from __future__ import annotations

import calendar
from collections import deque
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import fcntl
import glob
import logging
import os
import select
import termios
import threading
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Log level for the raw serial traffic, one step more detailed than DEBUG.
# Python and Home Assistant have no such level, so it is registered here and
# switched on with set_trace() instead of through the logger configuration.
TRACE = 5
if logging.getLevelName(TRACE) == f"Level {TRACE}":
    logging.addLevelName(TRACE, "TRACE")

_trace_users: set[str] = set()


def set_trace(user: str, enabled: bool) -> None:
    """Switch TRACE logging of the serial traffic on or off for one user.

    The traffic is logged while at least one user has it switched on.
    """
    if enabled:
        _trace_users.add(user)
    else:
        _trace_users.discard(user)
    _LOGGER.setLevel(TRACE if _trace_users else logging.NOTSET)


STX = 0x02
ETX = 0x03
ADDR_SOLARCOUNT = 0xB0
ADDR_HOST = 0xF0

CMD_INIT = 0x21
CMD_CHANNEL = 0x20
CMD_TIME = 0x04
RESPONSE_FLAG = 0x80

YIELD_UNIT_ADDRESS = ord("1")
YIELD_QUERY = ord("<")
YIELD_DAY = ord("D")
YIELD_HOUR = ord("h")
YIELD_MONTH = ord("M")
YIELD_YEAR = ord("Y")

MAX_CHANNELS = 6

# Timing taken from the firmware.
RESPONSE_TIMEOUT = 2.0  # SOLAR_FSM_STUCK_TIMEOUT
REQUEST_INTERVAL = 0.5  # SOLAR_READ_DELAY, pause before each channel request
YIELD_INTERVAL = 60.0  # SOLAR_TIME_CHECK_INTERVAL, time between yield rounds

# How long the last good value of a channel is kept after a fault.
HOLD_TIME = 60.0

# Throughput is averaged over this window.
THROUGHPUT_WINDOW = 60.0
# At 57600 baud with 8N1 every byte takes 10 bits on the line. RS485 is half
# duplex, so sending and receiving share this capacity.
BUS_CAPACITY = 57600 / 10  # bytes per second

# A yield response ends with two ETX. The frame is taken as complete at the
# first one; this is how long to wait for the second before sending again.
YIELD_SETTLE_TIME = 0.02
# Pause before each yield query. A SolarCount was seen to ignore a yield query
# sent right after the clock answer, and to answer with this pause.
YIELD_QUERY_PAUSE = 0.3
_YIELD_FRAME_LENGTH = 19  # STX up to and including the first ETX

_RESPONSE_HEADER = bytes([STX, ADDR_HOST, ADDR_SOLARCOUNT])
_MAX_FRAME_LENGTH = 64


class SolarCountError(Exception):
    """Base class for all errors of this module."""


class SolarCountConnectionError(SolarCountError):
    """The serial port cannot be opened or was lost."""


class SolarCountTimeoutError(SolarCountError):
    """The SolarCount did not answer in time."""


class SolarCountChecksumError(SolarCountError):
    """A response arrived but its checksum is wrong."""


class SolarCountUnexpectedResponseError(SolarCountError):
    """A valid response arrived, but it does not answer the request."""


@dataclass(frozen=True, slots=True)
class ChannelReading:
    """Measured values of one SolarCount channel."""

    power: int  # W, -32768 to 32767
    voltage: int  # V, 0 to 255


@dataclass(frozen=True, slots=True)
class ChannelYield:
    """Yields of one channel, as the raw numbers the SolarCount reports.

    A value is None if it could not be read in the last yield round.
    """

    previous_day: int | None
    previous_hour: int | None
    previous_month: int | None
    previous_year: int | None


@dataclass(frozen=True, slots=True)
class SolarCountClock:
    """Date and time of the SolarCount's own clock."""

    year: int  # two digits, 9 means 2009
    month: int
    day: int
    hour: int
    minute: int


@dataclass(frozen=True, slots=True)
class SolarCountData:
    """One measuring cycle. A channel is None if its response was invalid.

    ``yields`` is empty unless the client reads yields; it then holds the
    values of the most recent yield round.
    """

    channels: tuple[ChannelReading | None, ...]
    yields: tuple[ChannelYield, ...] = ()

    @property
    def total_power(self) -> int | None:
        """The power of all channels added up, None if any channel is unknown.

        A sum over only some of the channels would look like a real drop.
        """
        if any(reading is None for reading in self.channels):
            return None
        return sum(reading.power for reading in self.channels if reading is not None)


@dataclass(frozen=True, slots=True)
class SolarCountStats:
    """Counters of the RS485 traffic since the client was created.

    Every request ends in exactly one of the four outcomes, unless the serial
    port fails before the outcome is known.
    """

    requests: int = 0  # requests written to the serial port
    read_ok: int = 0  # the expected response arrived with a valid checksum
    crc_errors: int = 0  # a response arrived with a wrong checksum
    no_response: int = 0  # nothing arrived within the timeout
    unexpected: int = 0  # a valid response that does not answer the request
    bytes_sent: int = 0  # every byte written to the serial port
    bytes_received: int = 0  # every byte read, echoes and garbage included
    send_rate: float = 0.0  # B/s, average over the last minute
    receive_rate: float = 0.0  # B/s, average over the last minute
    bus_load: float = 0.0  # % of the bus capacity, both directions together


class TrafficMeter:
    """Counts the bytes on the serial line and averages them over a window.

    Until a full window has passed since the meter was created, the average
    is taken over the time that has passed so far.
    """

    def __init__(
        self,
        window: float = THROUGHPUT_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start counting from zero."""
        self._window = window
        self._clock = clock
        self._start = clock()
        self._events: deque[tuple[float, int, int]] = deque()
        self._lock = threading.Lock()
        self.bytes_sent = 0
        self.bytes_received = 0

    def sent(self, count: int) -> None:
        """Record ``count`` bytes written."""
        self._record(count, 0)

    def received(self, count: int) -> None:
        """Record ``count`` bytes read."""
        self._record(0, count)

    def _record(self, sent: int, received: int) -> None:
        with self._lock:
            self.bytes_sent += sent
            self.bytes_received += received
            self._events.append((self._clock(), sent, received))

    def reset(self) -> None:
        """Start counting from zero again, as if the meter were new."""
        with self._lock:
            self._start = self._clock()
            self._events.clear()
            self.bytes_sent = 0
            self.bytes_received = 0

    def rates(self) -> tuple[float, float]:
        """Return the send and receive rate in bytes per second."""
        now = self._clock()
        with self._lock:
            while self._events and self._events[0][0] <= now - self._window:
                self._events.popleft()
            sent = sum(event[1] for event in self._events)
            received = sum(event[2] for event in self._events)
        span = max(min(now - self._start, self._window), 1.0)
        return sent / span, received / span


def crc16(data: bytes) -> int:
    """CRC-16/CCITT with start value FFFF, as crc16_ccitt() in the firmware."""
    crc = 0xFFFF
    for byte in data:
        crc = ((crc >> 8) | (crc << 8)) & 0xFFFF
        crc ^= byte
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return crc


def build_request(command: int, data: bytes = b"") -> bytes:
    """Build a request frame from the host to the SolarCount."""
    length = 1 + len(data) + 2
    body = (
        bytes([ADDR_SOLARCOUNT, ADDR_HOST])
        + length.to_bytes(2, "little")
        + bytes([command])
        + data
    )
    return bytes([STX]) + body + crc16(body).to_bytes(2, "little") + bytes([ETX])


def find_response(buffer: bytes) -> bytes | None:
    """Return the first complete response frame in ``buffer``.

    Bytes in front of the frame are skipped: the RS485 line can deliver a
    stray 00 when the direction switches, and some adapters echo the request.
    Returns None while the frame is still incomplete.
    """
    start = 0
    while True:
        index = buffer.find(_RESPONSE_HEADER, start)
        if index < 0 or len(buffer) < index + 5:
            return None
        length = buffer[index + 3] | (buffer[index + 4] << 8)
        end = index + 5 + length  # position of ETX
        if not 3 <= length <= _MAX_FRAME_LENGTH:
            start = index + 1
            continue
        if len(buffer) <= end:
            return None
        if buffer[end] != ETX:
            start = index + 1
            continue
        frame = buffer[index : end + 1]
        if crc16(frame[1:-3]) != frame[-3] | (frame[-2] << 8):
            raise SolarCountChecksumError(f"wrong checksum in {frame.hex(' ')}")
        return frame


def check_response(frame: bytes, command: int, data: bytes = b"") -> None:
    """Raise unless ``frame`` is the answer to the request ``command data``."""
    if frame[5] != command | RESPONSE_FLAG:
        raise SolarCountUnexpectedResponseError(
            f"expected command {command | RESPONSE_FLAG:02x}: {frame.hex(' ')}"
        )
    if command == CMD_TIME and len(frame) < 14:
        raise SolarCountUnexpectedResponseError(
            f"not a full time response: {frame.hex(' ')}"
        )
    if command == CMD_CHANNEL and (len(frame) < 13 or frame[6] != data[0]):
        raise SolarCountUnexpectedResponseError(
            f"not a full response for channel {data[0]}: {frame.hex(' ')}"
        )


def parse_channel_response(frame: bytes, channel: int) -> ChannelReading:
    """Extract power and voltage from the response to a channel request."""
    check_response(frame, CMD_CHANNEL, bytes([channel]))
    # Firmware: chanWatt = rx[7] | rx[8] << 8, chanVolt = rx[11]. The firmware
    # keeps the power unsigned; here it is read as signed so that a negative
    # power arrives as a negative number.
    power = int.from_bytes(frame[7:9], "little", signed=True)
    return ChannelReading(power=power, voltage=frame[11])


def parse_time_response(frame: bytes) -> SolarCountClock:
    """Extract the SolarCount's date and time from the response to command 04."""
    check_response(frame, CMD_TIME)
    # Firmware: scDate = rx[6..10] = year, month, day, hour, minute
    clock = SolarCountClock(*frame[6:11])
    if not (
        1 <= clock.month <= 12
        and 1 <= clock.day <= 31
        and clock.hour <= 23
        and clock.minute <= 59
    ):
        raise SolarCountUnexpectedResponseError(
            f"not a valid date and time: {frame.hex(' ')}"
        )
    return clock


def previous_day(clock: SolarCountClock) -> int:
    """Return the day of the month before the SolarCount's current day."""
    if clock.day > 1:
        return clock.day - 1
    year, month = 2000 + clock.year, clock.month - 1
    if month == 0:
        year, month = year - 1, 12
    return calendar.monthrange(year, month)[1]


def previous_hour(clock: SolarCountClock) -> int:
    """Return the hour before the SolarCount's current hour.

    The firmware sends hour - 1 too, except that at midnight it sends 22;
    here midnight gives 23.
    """
    return (clock.hour - 1) % 24


def previous_month(clock: SolarCountClock) -> int:
    """Return the month before the SolarCount's current month."""
    return (clock.month - 2) % 12 + 1


def previous_year(clock: SolarCountClock) -> int:
    """Return the year before the SolarCount's current year, as two digits."""
    return (clock.year - 1) % 100


def crc8(data: bytes) -> int:
    """Checksum of the yield frames, as calculate_crc8() in the firmware."""
    crc = 0
    for byte in data:
        for bit in range(8):
            if (crc ^ (byte >> bit)) & 1:
                crc = ((crc ^ 0x18) >> 1) | 0x80
            else:
                crc >>= 1
    return crc | 0x80


def _yield_parameters(period: int, channel: int, value: int) -> bytes:
    """The six characters that select channel and day, hour, month or year.

    Day and hour are sent as two hex digits, like the firmware does. The month
    is sent as two decimal (BCD) digits, as the firmware's protocol notes
    describe it, so October is "10", not "0A". The notes say nothing about the
    year; it is sent like the month, so 2025 is "25".
    """
    if period in (YIELD_MONTH, YIELD_YEAR):
        return b"0%d00%02d" % (channel, value)
    return b"0%d00%02X" % (channel, value)


def build_yield_request(period: int, channel: int, value: int) -> bytes:
    """Build a yield query. ``value`` is the day, hour, month or year to ask for."""
    body = bytes([YIELD_UNIT_ADDRESS, YIELD_QUERY, period]) + _yield_parameters(
        period, channel, value
    )
    return bytes([STX]) + body + bytes([crc8(body), ETX])


def find_yield_response(buffer: bytes, period: int) -> bytes | None:
    """Return the first complete yield response for ``period`` in ``buffer``.

    The frame returned ends at the first ETX. Returns None while the frame is
    still incomplete. The echo of the request does not match, because it
    carries '<' where the response carries the period.
    """
    header = bytes([STX, YIELD_UNIT_ADDRESS, period])
    start = 0
    while True:
        index = buffer.find(header, start)
        if index < 0 or len(buffer) < index + _YIELD_FRAME_LENGTH:
            return None
        frame = buffer[index : index + _YIELD_FRAME_LENGTH]
        if frame[-1] != ETX:
            start = index + 1
            continue
        if crc8(frame[1:-2]) != frame[-2]:
            raise SolarCountChecksumError(f"wrong checksum in {frame.hex(' ')}")
        return frame


def parse_yield_response(frame: bytes, channel: int, value: int) -> int:
    """Extract the yield from the response to a yield query.

    The firmware keeps only the last four digits for a day and the last seven
    for an hour, and does not ask for months or years; all eight are read
    here, which gives the same number as long as the leading digits are zero.
    """
    digits = frame[9:17]
    if frame[3:9] != _yield_parameters(frame[2], channel, value) or not all(
        chr(digit) in "0123456789ABCDEFabcdef" for digit in digits
    ):
        raise SolarCountUnexpectedResponseError(
            f"not the yield of channel {channel} for {value}: {frame.hex(' ')}"
        )
    return int(digits, 16)


def list_serial_ports() -> list[str]:
    """Return the serial ports found on this machine, stable names first."""
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    covered = {os.path.realpath(path) for path in by_id}
    plain = [
        path
        for path in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        if path not in covered
    ]
    return by_id + plain


class _SerialPort:
    """A serial port opened at 57600 8N1 in raw mode."""

    def __init__(self, path: str, meter: TrafficMeter | None = None) -> None:
        self._name = os.path.basename(path)
        self._meter = meter
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as err:
            raise SolarCountConnectionError(f"cannot open {path}: {err}") from err
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attrs = termios.tcgetattr(self._fd)
            attrs[0] = 0  # input: no translation, no software flow control
            attrs[1] = 0  # output: no translation
            attrs[2] &= ~(
                termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS
            )
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0  # no echo, no line editing, no signals
            attrs[4] = attrs[5] = termios.B57600
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
            termios.tcflush(self._fd, termios.TCIOFLUSH)
        except BlockingIOError as err:
            self.close()
            raise SolarCountConnectionError(
                f"{path} is already in use by another program"
            ) from err
        except (OSError, termios.error) as err:
            self.close()
            raise SolarCountConnectionError(f"cannot configure {path}: {err}") from err

    def close(self) -> None:
        if self._fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = -1  # closed

    def discard_input(self) -> None:
        termios.tcflush(self._fd, termios.TCIFLUSH)

    def write(self, data: bytes) -> None:
        if _LOGGER.isEnabledFor(TRACE):
            _LOGGER.log(TRACE, "%s TX %s", self._name, data.hex(" "))
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        view = memoryview(data)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SolarCountConnectionError("serial port does not accept data")
            _, writable, _ = select.select([], [self._fd], [], remaining)
            if writable:
                written = os.write(self._fd, view)
                if self._meter is not None:
                    self._meter.sent(written)
                view = view[written:]
        termios.tcdrain(self._fd)

    def read(self, timeout: float) -> bytes:
        """Return the bytes that arrive within ``timeout`` (maybe none)."""
        readable, _, _ = select.select([self._fd], [], [], max(timeout, 0))
        if not readable:
            return b""
        try:
            data = os.read(self._fd, 256)
        except BlockingIOError:
            return b""
        if not data:
            raise SolarCountConnectionError("serial port was disconnected")
        if self._meter is not None:
            self._meter.received(len(data))
        if _LOGGER.isEnabledFor(TRACE):
            _LOGGER.log(TRACE, "%s RX %s", self._name, data.hex(" "))
        return data


class ValueHold:
    """Keeps the last good values of each channel for a while after a fault.

    A value that cannot be read is replaced by the last one that could, for up
    to ``hold_time`` seconds counted from the first failed read. After that it
    is None until it can be read again. This is tracked separately for the
    reading (power and voltage) and for each yield of every channel.
    """

    def __init__(
        self,
        hold_time: float = HOLD_TIME,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start with nothing held."""
        self._hold_time = hold_time
        self._clock = clock
        self._values: dict[tuple[str, int], Any] = {}
        self._fault_since: dict[tuple[str, int], float] = {}
        self._channels = 0
        self._yields = 0
        self._held: set[int] = set()

    @property
    def held_channels(self) -> tuple[int, ...]:
        """Channels that had a held value in the most recent result."""
        return tuple(sorted(self._held))

    def apply(self, data: SolarCountData) -> SolarCountData:
        """Fill the gaps in the result of a cycle with held values."""
        now = self._clock()
        self._held = set()
        self._channels = len(data.channels)
        self._yields = len(data.yields)
        return SolarCountData(
            channels=tuple(
                self._resolve(("reading", channel), reading, now)
                for channel, reading in enumerate(data.channels)
            ),
            yields=tuple(
                ChannelYield(
                    previous_day=self._resolve(
                        ("day", channel), item.previous_day, now
                    ),
                    previous_hour=self._resolve(
                        ("hour", channel), item.previous_hour, now
                    ),
                    previous_month=self._resolve(
                        ("month", channel), item.previous_month, now
                    ),
                    previous_year=self._resolve(
                        ("year", channel), item.previous_year, now
                    ),
                )
                for channel, item in enumerate(data.yields)
            ),
        )

    def after_failure(self) -> SolarCountData | None:
        """Return the held values after a cycle that failed as a whole.

        Returns None once no channel has a reading left to hold.
        """
        now = self._clock()
        self._held = set()
        channels = tuple(
            self._resolve(("reading", channel), None, now)
            for channel in range(self._channels)
        )
        yields = tuple(
            ChannelYield(
                previous_day=self._resolve(("day", channel), None, now),
                previous_hour=self._resolve(("hour", channel), None, now),
                previous_month=self._resolve(("month", channel), None, now),
                previous_year=self._resolve(("year", channel), None, now),
            )
            for channel in range(self._yields)
        )
        if all(reading is None for reading in channels):
            self._held = set()
            return None
        return SolarCountData(channels=channels, yields=yields)

    def _resolve[T](
        self, key: tuple[str, int], value: T | None, now: float
    ) -> T | None:
        """Return ``value``, or the held value while its hold time lasts."""
        if value is not None:
            self._values[key] = value
            self._fault_since.pop(key, None)
            return value
        if key not in self._values:
            return None
        if now - self._fault_since.setdefault(key, now) < self._hold_time:
            self._held.add(key[1])
            held: T = self._values[key]
            return held
        del self._values[key]
        del self._fault_since[key]
        return None


class EnergyIntegrator:
    """Adds up the generated energy from the power of completed read cycles.

    Between two completed cycles in a row the power is taken to change in a
    straight line (trapezoidal rule). A failed cycle, or one whose total power
    is unknown, interrupts the integration: the time until the next completed
    cycle is not counted. Negative power counts as zero, so the total never
    goes down.
    """

    def __init__(self) -> None:
        """Start at 0 kWh with no previous cycle."""
        self.total_kwh = 0.0
        self._last: tuple[float, int] | None = None

    def add(self, now: float, power: int | None) -> None:
        """Count a completed cycle. ``now`` is in seconds, ``power`` in watts."""
        if power is None:
            self.interrupt()
            return
        power = max(power, 0)
        if self._last is not None:
            then, last_power = self._last
            # W * s / 3 600 000 = kWh
            self.total_kwh += (last_power + power) / 2 * (now - then) / 3_600_000
        self._last = (now, power)

    def interrupt(self) -> None:
        """Do not count the time until the next completed cycle."""
        self._last = None


class SolarCountClient:
    """Reads power and voltage of the SolarCount channels over RS485."""

    def __init__(
        self,
        port: str,
        channels: int = MAX_CHANNELS,
        *,
        read_yields: bool = False,
        response_timeout: float = RESPONSE_TIMEOUT,
        request_interval: float = REQUEST_INTERVAL,
        yield_interval: float = YIELD_INTERVAL,
        yield_query_pause: float = YIELD_QUERY_PAUSE,
    ) -> None:
        """Remember the settings; the port is opened on first use."""
        if not 1 <= channels <= MAX_CHANNELS:
            raise ValueError(f"channels must be 1 to {MAX_CHANNELS}")
        self.port = port
        self.channels = channels
        self.read_yields = read_yields
        self._response_timeout = response_timeout
        self._request_interval = request_interval
        self._yield_interval = yield_interval
        self._yield_query_pause = yield_query_pause
        self._yields: tuple[ChannelYield, ...] = ()
        self._last_yield_round: float | None = None
        self._serial: _SerialPort | None = None
        self._lock = threading.Lock()
        self.traffic = TrafficMeter()
        self._counters = dict.fromkeys(
            ("requests", "read_ok", "crc_errors", "no_response", "unexpected"), 0
        )

    @property
    def stats(self) -> SolarCountStats:
        """Return the current counters. The setup probe is not counted."""
        send_rate, receive_rate = self.traffic.rates()
        return SolarCountStats(
            **self._counters,
            bytes_sent=self.traffic.bytes_sent,
            bytes_received=self.traffic.bytes_received,
            send_rate=round(send_rate, 1),
            receive_rate=round(receive_rate, 1),
            bus_load=round((send_rate + receive_rate) / BUS_CAPACITY * 100, 2),
        )

    def reset_stats(self) -> None:
        """Set all counters and the traffic meter back to zero.

        Waits for a running cycle to finish, so no request is half counted.
        """
        with self._lock:
            for name in self._counters:
                self._counters[name] = 0
            self.traffic.reset()

    def close(self) -> None:
        """Close the serial port."""
        with self._lock:
            self._close()

    def _close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def probe(self) -> None:
        """Open the port and check that a SolarCount answers. For the setup."""
        with self._lock:
            try:
                self._init(count=False)
            except (OSError, termios.error) as err:
                raise SolarCountConnectionError(
                    f"serial port {self.port} failed: {err}"
                ) from err
            finally:
                self._close()

    def read(self) -> SolarCountData:
        """Run one measuring cycle and return the values of all channels."""
        with self._lock:
            try:
                return self._read()
            except (OSError, termios.error) as err:
                self._close()
                raise SolarCountConnectionError(
                    f"serial port {self.port} failed: {err}"
                ) from err
            except SolarCountConnectionError:
                self._close()
                raise

    def _read(self) -> SolarCountData:
        self._open()
        if self.read_yields and self._yield_round_is_due():
            self._read_yields()
        self._init()
        readings: list[ChannelReading | None] = []
        for channel in range(self.channels):
            time.sleep(self._request_interval)
            try:
                frame = self._request(CMD_CHANNEL, bytes([channel]))
                readings.append(parse_channel_response(frame, channel))
            except (SolarCountConnectionError, SolarCountTimeoutError):
                raise
            except SolarCountError as err:
                # Firmware: a bad checksum only invalidates this channel.
                _LOGGER.debug("Channel %s: %s", channel, err)
                readings.append(None)
        if all(reading is None for reading in readings):
            raise SolarCountError("no channel returned a valid response")
        return SolarCountData(channels=tuple(readings), yields=self._yields)

    def _open(self) -> None:
        if self._serial is None:
            self._serial = _SerialPort(self.port, self.traffic)

    def _port(self) -> _SerialPort:
        """Return the open serial port."""
        if self._serial is None:
            raise SolarCountConnectionError(f"serial port {self.port} is not open")
        return self._serial

    def _init(self, *, count: bool = True) -> None:
        """Send the init command that starts every cycle in the firmware."""
        self._open()
        try:
            self._request(CMD_INIT, b"\x00", count=count)
        except SolarCountUnexpectedResponseError as err:
            # The firmware accepts any answer to the init, so carry on.
            _LOGGER.debug("Init: %s", err)

    def _yield_round_is_due(self) -> bool:
        return (
            self._last_yield_round is None
            or time.monotonic() - self._last_yield_round >= self._yield_interval
        )

    def _read_yields(self) -> None:
        """Read the clock, then the yields of the previous day, hour, month and year.

        A problem here never stops the power readings: a value that cannot be
        read becomes None, and the round ends at the first request the
        SolarCount does not answer.
        """
        self._last_yield_round = time.monotonic()
        days: list[int | None] = [None] * self.channels
        hours: list[int | None] = [None] * self.channels
        months: list[int | None] = [None] * self.channels
        years: list[int | None] = [None] * self.channels
        try:
            clock = self._exchange(
                build_request(CMD_TIME), find_response, parse_time_response
            )
            day, hour = previous_day(clock), previous_hour(clock)
            month, year = previous_month(clock), previous_year(clock)
            for channel in range(self.channels):
                days[channel] = self._read_yield(YIELD_DAY, channel, day)
            for channel in range(self.channels):
                hours[channel] = self._read_yield(YIELD_HOUR, channel, hour)
            for channel in range(self.channels):
                months[channel] = self._read_yield(YIELD_MONTH, channel, month)
            for channel in range(self.channels):
                years[channel] = self._read_yield(YIELD_YEAR, channel, year)
        except SolarCountConnectionError:
            raise
        except SolarCountError as err:
            _LOGGER.debug("Yield round stopped: %s", err)
        self._yields = tuple(
            ChannelYield(
                previous_day=day_value,
                previous_hour=hour_value,
                previous_month=month_value,
                previous_year=year_value,
            )
            for day_value, hour_value, month_value, year_value in zip(
                days, hours, months, years, strict=True
            )
        )

    def _read_yield(self, period: int, channel: int, value: int) -> int | None:
        """Read one yield. Returns None if the response is invalid."""
        if self._yield_query_pause > 0:
            time.sleep(self._yield_query_pause)
        try:
            return self._exchange(
                build_yield_request(period, channel, value),
                lambda buffer: find_yield_response(buffer, period),
                lambda frame: parse_yield_response(frame, channel, value),
            )
        except (SolarCountChecksumError, SolarCountUnexpectedResponseError) as err:
            _LOGGER.debug("Yield %s of channel %s: %s", chr(period), channel, err)
            return None
        finally:
            # Let the second ETX of the response arrive before sending again.
            self._port().read(YIELD_SETTLE_TIME)

    def _request(self, command: int, data: bytes = b"", *, count: bool = True) -> bytes:
        """Send a binary request and return its checked response frame."""

        def checked(frame: bytes) -> bytes:
            check_response(frame, command, data)
            return frame

        return self._exchange(
            build_request(command, data), find_response, checked, count=count
        )

    def _exchange[T](
        self,
        request: bytes,
        find: Callable[[bytes], bytes | None],
        parse: Callable[[bytes], T],
        *,
        count: bool = True,
    ) -> T:
        """Send a request, wait for its response and count the outcome.

        ``find`` picks the response frame out of the received bytes and
        ``parse`` checks that it answers the request.
        """
        serial = self._port()
        serial.discard_input()
        serial.write(request)
        outcome = "read_ok"
        try:
            return parse(self._wait_for_response(find))
        except SolarCountChecksumError:
            outcome = "crc_errors"
            raise
        except SolarCountTimeoutError:
            outcome = "no_response"
            raise
        except SolarCountUnexpectedResponseError:
            outcome = "unexpected"
            raise
        except BaseException:
            outcome = ""  # the serial port failed: no outcome to count
            raise
        finally:
            if count:
                self._counters["requests"] += 1
                if outcome:
                    self._counters[outcome] += 1

    def _wait_for_response(self, find: Callable[[bytes], bytes | None]) -> bytes:
        """Collect received bytes until ``find`` sees a response frame."""
        deadline = time.monotonic() + self._response_timeout
        buffer = b""
        while True:
            frame = find(buffer)
            if frame is not None:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SolarCountTimeoutError(
                    f"no answer from the SolarCount on {self.port}"
                )
            buffer += self._port().read(remaining)
