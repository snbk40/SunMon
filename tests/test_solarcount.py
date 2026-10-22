"""Tests for the SolarCount client. They need no Home Assistant and no hardware.

Run from the repository root with:  python3 -m unittest discover -s tests -v

The frames in FirmwareFrames are copied from SolarCountUART.c of the CP-JR
ARM7 LPC2368 firmware. The end-to-end tests talk to a simulated SolarCount on
a virtual serial port (Linux/macOS only).
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
import pty
import sys
import threading
import unittest

_API = (
    Path(__file__).resolve().parent.parent / "custom_components" / "sunmon" / "api.py"
)
_spec = importlib.util.spec_from_file_location("solarcount_api", _API)
assert _spec is not None and _spec.loader is not None
api = importlib.util.module_from_spec(_spec)
sys.modules["solarcount_api"] = api
_spec.loader.exec_module(api)


def h(text: str) -> bytes:
    return bytes.fromhex(text)


class FirmwareFrames(unittest.TestCase):
    """The client must produce and accept exactly what the firmware does."""

    def test_requests_match_firmware_byte_for_byte(self):
        self.assertEqual(api.build_request(0x21, b"\x00"), h("02B0F004002100CC3503"))
        self.assertEqual(api.build_request(0x04), h("02B0F0030004ABB903"))
        expected = [
            "02B0F004002000FD0603",
            "02B0F004002001DC1603",
            "02B0F004002002BF2603",
            "02B0F0040020039E3603",
            "02B0F004002004794603",
            "02B0F004002005585603",
        ]
        for channel, frame in enumerate(expected):
            self.assertEqual(api.build_request(0x20, bytes([channel])), h(frame))

    def test_captured_channel_response(self):
        # reqchan1_rsp in the firmware: a real capture, 135 W at 236 V.
        frame = h("02f0b00a00a00187000000ec00f23f03")
        self.assertEqual(api.find_response(frame), frame)
        reading = api.parse_channel_response(frame, 1)
        self.assertEqual((reading.power, reading.voltage), (135, 236))

    def test_power_is_read_as_signed_16_bit(self):
        cases = {
            "0000": 0,
            "0100": 1,
            "ff7f": 32767,
            "0080": -32768,
            "fbff": -5,
            "ffff": -1,
            "0040": 16384,
        }
        for raw, expected in cases.items():
            frame = response(0xA0, h("00" + raw + "0000ec00"))
            self.assertEqual(api.parse_channel_response(frame, 0).power, expected)

    def test_captured_init_response(self):
        frame = h("02f0b00900a1fd030000000007 2103".replace(" ", ""))
        self.assertEqual(api.find_response(frame), frame)

    def test_skips_stray_zero_and_echo(self):
        frame = h("02f0b00a00a00187000000ec00f23f03")
        echo = api.build_request(0x20, b"\x01")
        self.assertEqual(api.find_response(b"\x00" + echo + frame), frame)

    def test_incomplete_frame_waits(self):
        frame = h("02f0b00a00a00187000000ec00f23f03")
        for cut in range(len(frame)):
            self.assertIsNone(api.find_response(frame[:cut]))

    def test_wrong_checksum_is_rejected(self):
        with self.assertRaises(api.SolarCountChecksumError):
            api.find_response(h("02f0b00a00a00187000000ec00f23e03"))

    def test_response_for_other_channel_is_rejected(self):
        frame = h("02f0b00a00a00187000000ec00f23f03")
        with self.assertRaises(api.SolarCountError):
            api.parse_channel_response(frame, 0)


def response(command: int, data: bytes) -> bytes:
    body = (
        bytes([api.ADDR_HOST, api.ADDR_SOLARCOUNT])
        + (1 + len(data) + 2).to_bytes(2, "little")
        + bytes([command])
        + data
    )
    return b"\x02" + body + api.crc16(body).to_bytes(2, "little") + b"\x03"


class FakeSolarCount:
    """A SolarCount on a virtual serial port."""

    def __init__(
        self,
        watts,
        volts,
        *,
        echo=False,
        stray_zero=False,
        bad_crc_channels=(),
        silent=False,
        wrong_channels=(),
        short_channels=(),
        init_command=0xA1,
        clock=(26, 10, 11, 7, 33),
        day_yields=None,
        hour_yields=None,
        month_yields=None,
        year_yields=None,
        clock_silent=False,
        yields_silent=False,
        single_etx=False,
        yield_bad_crc_channels=(),
        yield_wrong_echo_channels=(),
    ):
        self.watts, self.volts = watts, volts
        self.echo, self.stray_zero = echo, stray_zero
        self.bad_crc_channels, self.silent = bad_crc_channels, silent
        # Valid frames that do not answer the request:
        self.wrong_channels = wrong_channels  # answer names another channel
        self.short_channels = short_channels  # answer lacks power and voltage
        self.init_command = init_command  # command byte of the init answer
        # Clock and yields:
        self.clock = clock  # year, month, day, hour, minute
        self.day_yields = day_yields or [0] * 6
        self.hour_yields = hour_yields or [0] * 6
        self.month_yields = month_yields or [0] * 6
        self.year_yields = year_yields or [0] * 6
        self.clock_silent, self.yields_silent = clock_silent, yields_silent
        self.year_silent = False  # no answer to year queries only
        self.single_etx = single_etx  # end yield answers with one ETX only
        self.yield_bad_crc_channels = yield_bad_crc_channels
        self.yield_wrong_echo_channels = yield_wrong_echo_channels
        self.requests: list[bytes] = []
        self._master, self._slave = pty.openpty()
        self.port = os.ttyname(self._slave)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        os.close(self._master)
        os.close(self._slave)

    def _run(self):
        buffer = b""
        while True:
            try:
                chunk = os.read(self._master, 256)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                if len(buffer) >= 2 and buffer[1] == api.YIELD_UNIT_ADDRESS:
                    size = 12  # yield query
                elif len(buffer) >= 5:
                    size = 6 + (buffer[3] | buffer[4] << 8)
                else:
                    break
                if len(buffer) < size:
                    break
                request, buffer = buffer[:size], buffer[size:]
                self.requests.append(request)
                answer = b"" if self.silent else self._answer(request)
                if answer:
                    os.write(self._master, answer)

    def _answer_yield(self, request: bytes) -> bytes:
        period, channel = request[3], request[5] - 0x30
        if self.yields_silent or (self.year_silent and period == api.YIELD_YEAR):
            return b""
        values = {
            api.YIELD_DAY: self.day_yields,
            api.YIELD_HOUR: self.hour_yields,
            api.YIELD_MONTH: self.month_yields,
            api.YIELD_YEAR: self.year_yields,
        }[period]
        echo = request[4:10]
        if channel in self.yield_wrong_echo_channels:
            echo = b"0%d00FF" % ((channel + 1) % 6)
        body = (
            bytes([api.YIELD_UNIT_ADDRESS, period]) + echo + b"%08X" % values[channel]
        )
        crc = api.crc8(body)
        if channel in self.yield_bad_crc_channels:
            crc ^= 0x01
        return (
            b"\x02"
            + body
            + bytes([crc])
            + (b"\x03" if self.single_etx else b"\x03\x03")
        )

    def _answer(self, request: bytes) -> bytes:
        if request[1] == api.YIELD_UNIT_ADDRESS:
            answer = self._answer_yield(request)
            if not answer:
                return b""
            prefix = (b"\x00" if self.stray_zero else b"") + (
                request if self.echo else b""
            )
            return prefix + answer
        command = request[5]
        if command == api.CMD_TIME:
            if self.clock_silent:
                return b""
            answer = response(0x84, bytes(self.clock))
        elif command == api.CMD_INIT:
            answer = response(self.init_command, h("fd0300000000"))
        elif request[6] in self.short_channels:
            answer = response(0xA0, bytes([request[6]]))
        else:
            channel = request[6]
            named = (channel + 1) % 6 if channel in self.wrong_channels else channel
            data = (
                bytes([named])
                + self.watts[channel].to_bytes(2, "little", signed=True)
                + b"\x00\x00"
                + bytes([self.volts[channel], 0])
            )
            answer = response(0xA0, data)
            if channel in self.bad_crc_channels:
                answer = answer[:-3] + bytes([answer[-3] ^ 0xFF]) + answer[-2:]
        prefix = (b"\x00" if self.stray_zero else b"") + (request if self.echo else b"")
        return prefix + answer


# Includes bytes a terminal would treat specially (CR, LF, XON, XOFF, Ctrl-C,
# Ctrl-D, DEL) to prove the port really is in raw mode.
WATTS = [135, 0x0D0A, 0x1311, 0x4000, -1, -31000]
VOLTS = [236, 0x7F, 0x04, 0x03, 0xFF, 0x11]


@unittest.skipUnless(hasattr(os, "ttyname"), "needs a POSIX system")
class EndToEnd(unittest.TestCase):
    def client(self, device, channels=6):
        client = api.SolarCountClient(
            device.port, channels, response_timeout=0.4, request_interval=0.01
        )
        self.addCleanup(device.close)
        self.addCleanup(client.close)
        return client

    def test_reads_all_six_channels(self):
        device = FakeSolarCount(WATTS, VOLTS)
        data = self.client(device).read()
        self.assertEqual([c.power for c in data.channels], WATTS)
        self.assertEqual([c.voltage for c in data.channels], VOLTS)
        # Same sequence as the firmware: init, then channel 0 to 5.
        self.assertEqual(device.requests[0], h("02B0F004002100CC3503"))
        self.assertEqual(
            device.requests[1:],
            [api.build_request(0x20, bytes([n])) for n in range(6)],
        )

    def test_repeated_cycles_reuse_the_port(self):
        device = FakeSolarCount(WATTS, VOLTS)
        client = self.client(device, channels=3)
        for _ in range(3):
            data = client.read()
            self.assertEqual([c.power for c in data.channels], WATTS[:3])
        self.assertEqual(len(device.requests), 3 * 4)

    def test_tolerates_echo_and_stray_zero(self):
        device = FakeSolarCount(WATTS, VOLTS, echo=True, stray_zero=True)
        data = self.client(device).read()
        self.assertEqual([c.power for c in data.channels], WATTS)

    def test_bad_checksum_invalidates_only_that_channel(self):
        device = FakeSolarCount(WATTS, VOLTS, bad_crc_channels=(2,))
        data = self.client(device).read()
        self.assertIsNone(data.channels[2])
        self.assertEqual(data.channels[1].power, WATTS[1])
        self.assertEqual(data.channels[3].power, WATTS[3])

    def test_silent_device_times_out(self):
        device = FakeSolarCount(WATTS, VOLTS, silent=True)
        client = self.client(device)
        with self.assertRaises(api.SolarCountTimeoutError):
            client.read()
        with self.assertRaises(api.SolarCountTimeoutError):
            client.probe()

    def test_probe_succeeds_and_releases_the_port(self):
        device = FakeSolarCount(WATTS, VOLTS)
        client = self.client(device)
        client.probe()
        client.probe()
        self.assertEqual(client.read().channels[0].power, WATTS[0])

    def test_missing_port(self):
        client = api.SolarCountClient("/dev/does-not-exist")
        with self.assertRaises(api.SolarCountConnectionError):
            client.read()
        with self.assertRaises(api.SolarCountConnectionError):
            client.probe()

    def test_port_in_use(self):
        device = FakeSolarCount(WATTS, VOLTS)
        first = self.client(device)
        first.read()
        second = api.SolarCountClient(device.port, response_timeout=0.4)
        with self.assertRaises(api.SolarCountConnectionError):
            second.read()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=0)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@unittest.skipUnless(hasattr(os, "ttyname"), "needs a POSIX system")
class TraceLogging(unittest.TestCase):
    def setUp(self):
        self.capture = _Capture()
        api._LOGGER.addHandler(self.capture)
        self.addCleanup(api._LOGGER.removeHandler, self.capture)
        self.addCleanup(api.set_trace, "test", False)
        self.addCleanup(api.set_trace, "other", False)
        self.device = FakeSolarCount(WATTS, VOLTS)
        self.client = api.SolarCountClient(
            self.device.port, 2, response_timeout=0.4, request_interval=0.01
        )
        self.addCleanup(self.device.close)
        self.addCleanup(self.client.close)

    def traffic(self):
        return [r for r in self.capture.records if r.levelno == api.TRACE]

    def test_level_is_called_trace_and_sits_below_debug(self):
        self.assertEqual(logging.getLevelName(api.TRACE), "TRACE")
        self.assertLess(api.TRACE, logging.DEBUG)

    def test_nothing_is_traced_by_default(self):
        self.client.read()
        self.assertEqual(self.traffic(), [])

    def test_debug_logging_alone_does_not_trace(self):
        api._LOGGER.setLevel(logging.DEBUG)
        self.addCleanup(api._LOGGER.setLevel, logging.NOTSET)
        self.client.read()
        self.assertEqual(self.traffic(), [])

    def test_traces_every_byte_sent_and_received(self):
        api.set_trace("test", True)
        self.client.read()
        records = self.traffic()
        self.assertTrue(all(r.levelname == "TRACE" for r in records))
        port = os.path.basename(self.device.port)
        sent = [r.getMessage() for r in records if " TX " in r.getMessage()]
        self.assertEqual(
            sent,
            [
                f"{port} TX 02 b0 f0 04 00 21 00 cc 35 03",
                f"{port} TX 02 b0 f0 04 00 20 00 fd 06 03",
                f"{port} TX 02 b0 f0 04 00 20 01 dc 16 03",
            ],
        )
        received = b"".join(
            bytes.fromhex(r.getMessage().split(" RX ")[1])
            for r in records
            if " RX " in r.getMessage()
        )
        expected = response(0xA1, h("fd0300000000"))
        for channel in range(2):
            data = (
                bytes([channel])
                + WATTS[channel].to_bytes(2, "little", signed=True)
                + b"\x00\x00"
                + bytes([VOLTS[channel], 0])
            )
            expected += response(0xA0, data)
        self.assertEqual(received, expected)

    def test_trace_stays_on_until_the_last_user_switches_it_off(self):
        api.set_trace("test", True)
        api.set_trace("other", True)
        api.set_trace("test", False)
        self.client.read()
        self.assertTrue(self.traffic())
        self.capture.records.clear()
        api.set_trace("other", False)
        self.client.read()
        self.assertEqual(self.traffic(), [])


@unittest.skipUnless(hasattr(os, "ttyname"), "needs a POSIX system")
class Counters(unittest.TestCase):
    def client(self, device, channels=6):
        client = api.SolarCountClient(
            device.port, channels, response_timeout=0.4, request_interval=0.01
        )
        self.addCleanup(device.close)
        self.addCleanup(client.close)
        return client

    def assertStats(self, client, **expected):
        stats = client.stats
        got = {
            "requests": stats.requests,
            "read_ok": stats.read_ok,
            "crc_errors": stats.crc_errors,
            "no_response": stats.no_response,
            "unexpected": stats.unexpected,
        }
        want = dict.fromkeys(got, 0) | expected
        self.assertEqual(got, want)
        # Every request ends in exactly one outcome.
        self.assertEqual(sum(got.values()) - got["requests"], got["requests"])

    def test_start_at_zero(self):
        self.assertStats(api.SolarCountClient("/dev/does-not-exist"))

    def test_healthy_cycle_counts_init_and_every_channel(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS))
        client.read()
        self.assertStats(client, requests=7, read_ok=7)
        client.read()
        self.assertStats(client, requests=14, read_ok=14)

    def test_fewer_channels(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS), channels=2)
        client.read()
        self.assertStats(client, requests=3, read_ok=3)

    def test_crc_error(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, bad_crc_channels=(1, 4)))
        client.read()
        self.assertStats(client, requests=7, read_ok=5, crc_errors=2)

    def test_no_response_stops_the_cycle_at_the_first_request(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, silent=True))
        for cycle in (1, 2, 3):
            with self.assertRaises(api.SolarCountTimeoutError):
                client.read()
            self.assertStats(client, requests=cycle, no_response=cycle)

    def test_device_goes_silent_and_comes_back(self):
        device = FakeSolarCount(WATTS, VOLTS)
        client = self.client(device, channels=2)
        client.read()
        device.silent = True
        with self.assertRaises(api.SolarCountTimeoutError):
            client.read()
        device.silent = False
        client.read()
        self.assertStats(client, requests=7, read_ok=6, no_response=1)

    def test_response_for_another_channel_is_unexpected(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, wrong_channels=(2,)))
        data = client.read()
        self.assertIsNone(data.channels[2])
        self.assertEqual(data.channels[3].power, WATTS[3])
        self.assertStats(client, requests=7, read_ok=6, unexpected=1)

    def test_short_channel_response_is_unexpected(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, short_channels=(0,)))
        data = client.read()
        self.assertIsNone(data.channels[0])
        self.assertStats(client, requests=7, read_ok=6, unexpected=1)

    def test_unexpected_init_response_is_counted_but_the_cycle_continues(self):
        # 0x84 is the answer to the time query, as in the firmware's init1_rsp.
        client = self.client(FakeSolarCount(WATTS, VOLTS, init_command=0x84))
        data = client.read()
        self.assertEqual([c.power for c in data.channels], WATTS)
        self.assertStats(client, requests=7, read_ok=6, unexpected=1)

    def test_setup_probe_is_not_counted(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS))
        client.probe()
        self.assertStats(client)
        silent = self.client(FakeSolarCount(WATTS, VOLTS, silent=True))
        with self.assertRaises(api.SolarCountTimeoutError):
            silent.probe()
        self.assertStats(silent)

    def test_probe_accepts_an_unexpected_init_response(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, init_command=0x84))
        client.probe()

    def test_missing_port_counts_nothing(self):
        client = api.SolarCountClient("/dev/does-not-exist")
        with self.assertRaises(api.SolarCountConnectionError):
            client.read()
        self.assertStats(client)

    def test_reset_stats_sets_everything_to_zero(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, bad_crc_channels=(1,)))
        client.read()
        client.reset_stats()
        self.assertStats(client)
        stats = client.stats
        self.assertEqual(
            (stats.bytes_sent, stats.bytes_received, stats.send_rate, stats.bus_load),
            (0, 0, 0.0, 0.0),
        )
        client.read()
        self.assertStats(client, requests=7, read_ok=6, crc_errors=1)

    def test_error_rate_is_unknown_before_the_first_request(self):
        self.assertIsNone(api.SolarCountClient("/dev/does-not-exist").stats.error_rate)

    def test_error_rate_is_zero_on_a_healthy_line(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS))
        client.read()
        self.assertEqual(client.stats.error_rate, 0.0)

    def test_error_rate_counts_every_kind_of_failure(self):
        client = api.SolarCountClient("/dev/does-not-exist")
        client._counters.update(
            requests=10, read_ok=6, crc_errors=2, no_response=1, unexpected=1
        )
        self.assertEqual(client.stats.error_rate, 40.0)

    def test_error_rate_leaves_out_requests_without_outcome(self):
        client = api.SolarCountClient("/dev/does-not-exist")
        # 3 requests, one cut short by a failing serial port.
        client._counters.update(requests=3, read_ok=1, crc_errors=1)
        self.assertEqual(client.stats.error_rate, 50.0)

    def test_error_rate_is_rounded_to_two_decimals(self):
        client = api.SolarCountClient("/dev/does-not-exist")
        client._counters.update(requests=7, read_ok=6, crc_errors=1)
        self.assertEqual(client.stats.error_rate, 14.29)

    def test_error_rate_with_crc_error_and_reset(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, bad_crc_channels=(1,)))
        client.read()
        self.assertEqual(client.stats.error_rate, 14.29)  # 1 of 7
        client.reset_stats()
        self.assertIsNone(client.stats.error_rate)

    def test_stats_are_a_snapshot(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS), channels=1)
        before = client.stats
        client.read()
        self.assertEqual(before.requests, 0)
        self.assertEqual(client.stats.requests, 2)
        with self.assertRaises(AttributeError):
            before.requests = 5


class YieldFrames(unittest.TestCase):
    """Checksum and frames of the yield queries, against real captures."""

    # Captured exchange from the firmware source: month 1 of channel 1.
    MONTH_QUERY = "02 31 3c 4d 30 31 30 30 30 31 b0 03"
    MONTH_RESPONSE = "02 31 4d 30 31 30 30 30 31 30 30 30 30 31 34 38 36 aa 03 03"
    # yield_minute_rsp in the firmware: minute 2 of channel 1.
    MINUTE_RESPONSE = "02 31 6d 30 31 30 30 30 32 30 30 30 30 30 32 41 44 c8 03 03"

    def test_request_matches_capture_byte_for_byte(self):
        self.assertEqual(api.build_yield_request(ord("M"), 1, 1), h(self.MONTH_QUERY))

    def test_day_and_hour_are_sent_as_two_hex_digits(self):
        self.assertEqual(
            api.build_yield_request(api.YIELD_DAY, 5, 31)[3:10], b"D05001F"
        )
        self.assertEqual(
            api.build_yield_request(api.YIELD_HOUR, 0, 9)[3:10], b"h000009"
        )
        self.assertEqual(
            api.build_yield_request(api.YIELD_HOUR, 2, 23)[3:10], b"h020017"
        )

    def test_month_is_sent_as_two_decimal_digits(self):
        for month, digits in {1: b"01", 9: b"09", 10: b"10", 12: b"12"}.items():
            request = api.build_yield_request(api.YIELD_MONTH, 3, month)
            self.assertEqual(request[3:10], b"M0300" + digits)

    def test_month_answer_is_matched_with_decimal_digits(self):
        body = b"1M030010" + b"0000002A"
        frame = b"\x02" + body + bytes([api.crc8(body)]) + b"\x03"
        self.assertEqual(api.parse_yield_response(frame, 3, 10), 42)
        body = b"1M03000A" + b"0000002A"
        frame = b"\x02" + body + bytes([api.crc8(body)]) + b"\x03"
        with self.assertRaises(api.SolarCountUnexpectedResponseError):
            api.parse_yield_response(frame, 3, 10)

    def test_captured_responses(self):
        frame = api.find_yield_response(h(self.MONTH_RESPONSE), ord("M"))
        self.assertEqual(frame, h(self.MONTH_RESPONSE)[:19])
        self.assertEqual(api.parse_yield_response(frame, 1, 1), 0x1486)
        frame = api.find_yield_response(h(self.MINUTE_RESPONSE), ord("m"))
        self.assertEqual(api.parse_yield_response(frame, 1, 2), 0x2AD)

    def test_complete_after_the_first_etx(self):
        data = h(self.MONTH_RESPONSE)
        for cut in range(19):
            self.assertIsNone(api.find_yield_response(data[:cut], ord("M")))
        self.assertIsNotNone(api.find_yield_response(data[:19], ord("M")))

    def test_skips_stray_zero_and_echo(self):
        data = b"\x00" + h(self.MONTH_QUERY) + h(self.MONTH_RESPONSE)
        self.assertEqual(
            api.find_yield_response(data, ord("M")), h(self.MONTH_RESPONSE)[:19]
        )

    def test_wrong_checksum_is_rejected(self):
        data = bytearray(h(self.MONTH_RESPONSE))
        data[14] ^= 0x01
        with self.assertRaises(api.SolarCountChecksumError):
            api.find_yield_response(bytes(data), ord("M"))

    def test_answer_for_another_channel_or_day_is_rejected(self):
        frame = h(self.MONTH_RESPONSE)[:19]
        for channel, value in ((2, 1), (1, 2)):
            with self.assertRaises(api.SolarCountUnexpectedResponseError):
                api.parse_yield_response(frame, channel, value)

    def test_non_hex_yield_is_rejected(self):
        body = b"1D010002" + b"00-0002A"
        frame = b"\x02" + body + bytes([api.crc8(body)]) + b"\x03"
        with self.assertRaises(api.SolarCountUnexpectedResponseError):
            api.parse_yield_response(frame, 1, 2)

    def test_time_response(self):
        # init1_rsp in the firmware holds a captured time response with a valid
        # checksum (2009-12-04 10:38), padded there with 00 before the ETX.
        frame = api.find_response(h("02 f0 b0 08 00 84 09 0c 04 0a 26 86 cd 03"))
        clock = api.parse_time_response(frame)
        self.assertEqual(
            (clock.year, clock.month, clock.day, clock.hour, clock.minute),
            (9, 12, 4, 10, 38),
        )

    def test_impossible_time_is_rejected(self):
        with self.assertRaises(api.SolarCountUnexpectedResponseError):
            api.parse_time_response(response(0x84, bytes([26, 13, 1, 0, 0])))

    def test_previous_day(self):
        cases = {
            (26, 10, 11): 10,
            (26, 10, 1): 30,
            (26, 11, 1): 31,
            (26, 1, 1): 31,
            (26, 3, 1): 28,
            (28, 3, 1): 29,
        }
        for (year, month, day), expected in cases.items():
            clock = api.SolarCountClock(year, month, day, 12, 0)
            self.assertEqual(api.previous_day(clock), expected, (year, month, day))

    def test_previous_hour(self):
        for hour, expected in {0: 23, 1: 0, 12: 11, 23: 22}.items():
            clock = api.SolarCountClock(26, 10, 11, hour, 0)
            self.assertEqual(api.previous_hour(clock), expected)

    def test_previous_month(self):
        for month, expected in {1: 12, 2: 1, 10: 9, 12: 11}.items():
            clock = api.SolarCountClock(26, month, 11, 12, 0)
            self.assertEqual(api.previous_month(clock), expected)

    def test_previous_year(self):
        for year, expected in {26: 25, 10: 9, 1: 0, 0: 99}.items():
            clock = api.SolarCountClock(year, 6, 11, 12, 0)
            self.assertEqual(api.previous_year(clock), expected)


DAY_YIELDS = [685, 0, 70000, 5254, 1, 0xFFFFFFFF]
HOUR_YIELDS = [41100, 0, 9, 123456789, 60, 7]
MONTH_YIELDS = [5254, 0, 1, 0x7FFFFFFF, 999999, 12]
YEAR_YIELDS = [61234, 0, 2, 0x10000000, 77, 4242]


@unittest.skipUnless(hasattr(os, "ttyname"), "needs a POSIX system")
class Yields(unittest.TestCase):
    def client(self, device, channels=6, **kwargs):
        kwargs.setdefault("yield_query_pause", 0)
        client = api.SolarCountClient(
            device.port,
            channels,
            read_yields=True,
            response_timeout=0.3,
            request_interval=0.01,
            **kwargs,
        )
        self.addCleanup(device.close)
        self.addCleanup(client.close)
        return client

    def device(self, **kwargs):
        return FakeSolarCount(
            WATTS,
            VOLTS,
            day_yields=DAY_YIELDS,
            hour_yields=HOUR_YIELDS,
            month_yields=MONTH_YIELDS,
            year_yields=YEAR_YIELDS,
            **kwargs,
        )

    def stats(self, client):
        s = client.stats
        return (s.requests, s.read_ok, s.crc_errors, s.no_response, s.unexpected)

    def test_reads_day_hour_month_and_year_yield_of_every_channel(self):
        device = self.device(clock=(26, 10, 11, 7, 33))
        client = self.client(device)
        data = client.read()
        self.assertEqual([y.previous_day for y in data.yields], DAY_YIELDS)
        self.assertEqual([y.previous_hour for y in data.yields], HOUR_YIELDS)
        self.assertEqual([y.previous_month for y in data.yields], MONTH_YIELDS)
        self.assertEqual([y.previous_year for y in data.yields], YEAR_YIELDS)
        self.assertEqual([c.power for c in data.channels], WATTS)
        # Clock, six days, six hours, six months, six years, then the usual
        # init and six channels.
        self.assertEqual(self.stats(client), (32, 32, 0, 0, 0))
        requests = device.requests
        self.assertEqual(requests[0], h("02B0F0030004ABB903"))
        self.assertEqual(
            requests[1:7],
            [api.build_yield_request(api.YIELD_DAY, n, 10) for n in range(6)],
        )
        self.assertEqual(
            requests[7:13],
            [api.build_yield_request(api.YIELD_HOUR, n, 6) for n in range(6)],
        )
        self.assertEqual(
            requests[13:19],
            [api.build_yield_request(api.YIELD_MONTH, n, 9) for n in range(6)],
        )
        self.assertEqual(
            requests[19:25],
            [api.build_yield_request(api.YIELD_YEAR, n, 25) for n in range(6)],
        )
        self.assertEqual(requests[19][3:10], b"Y000025")
        self.assertEqual(requests[25], h("02B0F004002100CC3503"))

    def test_asks_for_the_last_day_of_the_previous_month_and_hour_23(self):
        device = self.device(clock=(26, 3, 1, 0, 5))
        self.client(device, channels=1).read()
        self.assertEqual(
            device.requests[1], api.build_yield_request(api.YIELD_DAY, 0, 28)
        )
        self.assertEqual(
            device.requests[2], api.build_yield_request(api.YIELD_HOUR, 0, 23)
        )
        self.assertEqual(
            device.requests[3], api.build_yield_request(api.YIELD_MONTH, 0, 2)
        )
        self.assertEqual(
            device.requests[4], api.build_yield_request(api.YIELD_YEAR, 0, 25)
        )

    def test_unanswered_year_query_keeps_the_other_yields(self):
        device = self.device()
        device.year_silent = True
        client = self.client(device)
        data = client.read()
        self.assertEqual([y.previous_month for y in data.yields], MONTH_YIELDS)
        self.assertTrue(all(y.previous_year is None for y in data.yields))
        self.assertEqual([c.power for c in data.channels], WATTS)
        # The round stops at the first year query.
        self.assertEqual(self.stats(client), (27, 26, 0, 1, 0))

    def test_asks_for_december_in_january(self):
        device = self.device(clock=(27, 1, 15, 12, 0))
        self.client(device, channels=1).read()
        self.assertEqual(
            device.requests[3], api.build_yield_request(api.YIELD_MONTH, 0, 12)
        )
        self.assertEqual(device.requests[3][3:10], b"M000012")

    def test_yields_are_read_once_per_interval_and_kept_in_between(self):
        device = self.device()
        client = self.client(device, channels=2, yield_interval=3600)
        first = client.read()
        device.day_yields = [1, 2, 3, 4, 5, 6]
        second = client.read()
        self.assertEqual(second.yields, first.yields)
        self.assertEqual(self.stats(client)[0], (1 + 2 + 2 + 2 + 2 + 3) + 3)

    def test_yields_are_read_again_when_the_interval_has_passed(self):
        device = self.device()
        client = self.client(device, channels=2, yield_interval=0)
        client.read()
        device.day_yields = [1, 2, 3, 4, 5, 6]
        self.assertEqual([y.previous_day for y in client.read().yields], [1, 2])

    def test_pauses_before_each_yield_query(self):
        self.assertEqual(api.YIELD_QUERY_PAUSE, 0.3)
        device = self.device()
        client = self.client(device, channels=1, yield_query_pause=0.05)
        sleeps = []
        original = api.time.sleep

        def sleep(seconds):
            sleeps.append(seconds)
            original(seconds)

        api.time.sleep = sleep
        self.addCleanup(setattr, api.time, "sleep", original)
        data = client.read()
        self.assertEqual(data.yields[0].previous_day, DAY_YIELDS[0])
        # Four yield queries, then the pause before the one channel request.
        self.assertEqual(sleeps, [0.05, 0.05, 0.05, 0.05, 0.01])

    def test_not_read_unless_switched_on(self):
        device = self.device()
        client = api.SolarCountClient(
            device.port, 2, response_timeout=0.3, request_interval=0.01
        )
        self.addCleanup(device.close)
        self.addCleanup(client.close)
        self.assertEqual(client.read().yields, ())
        self.assertEqual(len(device.requests), 3)

    def test_tolerates_echo_stray_zero_and_a_single_etx(self):
        for options in ({"echo": True, "stray_zero": True}, {"single_etx": True}):
            device = self.device(**options)
            data = self.client(device).read()
            self.assertEqual([y.previous_day for y in data.yields], DAY_YIELDS, options)
            self.assertEqual(
                [y.previous_hour for y in data.yields], HOUR_YIELDS, options
            )
            self.assertEqual(
                [y.previous_month for y in data.yields], MONTH_YIELDS, options
            )
            self.assertEqual(
                [y.previous_year for y in data.yields], YEAR_YIELDS, options
            )

    def test_bad_checksum_invalidates_only_that_channels_yields(self):
        device = self.device(yield_bad_crc_channels=(3,))
        client = self.client(device)
        data = client.read()
        self.assertEqual(data.yields[3], api.ChannelYield(None, None, None, None))
        self.assertEqual(data.yields[2].previous_day, DAY_YIELDS[2])
        self.assertEqual(data.yields[4].previous_hour, HOUR_YIELDS[4])
        self.assertEqual(data.yields[5].previous_month, MONTH_YIELDS[5])
        self.assertEqual(data.yields[0].previous_year, YEAR_YIELDS[0])
        self.assertEqual(self.stats(client), (32, 28, 4, 0, 0))

    def test_answer_for_another_channel_is_unexpected(self):
        device = self.device(yield_wrong_echo_channels=(0,))
        client = self.client(device)
        data = client.read()
        self.assertEqual(data.yields[0], api.ChannelYield(None, None, None, None))
        self.assertEqual(self.stats(client), (32, 28, 0, 0, 4))

    def test_silent_on_yields_does_not_stop_the_power_readings(self):
        device = self.device(yields_silent=True)
        client = self.client(device)
        data = client.read()
        self.assertEqual([c.power for c in data.channels], WATTS)
        self.assertTrue(
            all(y == api.ChannelYield(None, None, None, None) for y in data.yields)
        )
        # Clock answered, the round stopped at the first unanswered query.
        self.assertEqual(self.stats(client), (9, 8, 0, 1, 0))

    def test_silent_on_the_clock_skips_the_yields(self):
        device = self.device(clock_silent=True)
        client = self.client(device)
        data = client.read()
        self.assertEqual([c.power for c in data.channels], WATTS)
        self.assertTrue(
            all(y == api.ChannelYield(None, None, None, None) for y in data.yields)
        )
        self.assertEqual(self.stats(client), (8, 7, 0, 1, 0))

    def test_yields_that_stop_answering_become_unknown(self):
        device = self.device()
        client = self.client(device, channels=2, yield_interval=0)
        self.assertEqual(client.read().yields[0].previous_day, DAY_YIELDS[0])
        device.yields_silent = True
        self.assertEqual(
            client.read().yields[0], api.ChannelYield(None, None, None, None)
        )

    def test_completely_silent_device_still_times_out(self):
        device = self.device(silent=True)
        client = self.client(device)
        with self.assertRaises(api.SolarCountTimeoutError):
            client.read()


class Hold(unittest.TestCase):
    """Last good values are kept for a minute after a fault."""

    def setUp(self):
        self.now = 1000.0
        self.hold = api.ValueHold(hold_time=60, clock=lambda: self.now)

    def data(self, powers, days=None, hours=None, months=None, years=None):
        channels = tuple(
            None if power is None else api.ChannelReading(power, 230)
            for power in powers
        )
        yields = ()
        if days is not None:
            months = months or [7] * len(days)
            years = years or [8] * len(days)
            yields = tuple(
                api.ChannelYield(d, h, m, y)
                for d, h, m, y in zip(days, hours, months, years, strict=True)
            )
        return api.SolarCountData(channels=channels, yields=yields)

    def powers(self, data):
        return [None if c is None else c.power for c in data.channels]

    def test_good_values_pass_through(self):
        data = self.data([100, 200, 300])
        self.assertEqual(self.hold.apply(data), data)

    def test_failed_channel_keeps_its_last_value_for_a_minute(self):
        self.hold.apply(self.data([100, 200, 300]))
        for elapsed in (10, 30, 59.9):
            self.now = 1010 + elapsed
            held = self.hold.apply(self.data([110, None, 310]))
            self.assertEqual(self.powers(held), [110, 200, 310], elapsed)
            self.assertEqual(held.channels[1].voltage, 230)

    def test_the_minute_counts_from_the_first_failed_read(self):
        self.hold.apply(self.data([100, 200]))
        self.now = 1040  # 40 s without a fault do not use up the hold
        self.hold.apply(self.data([100, 200]))
        self.now = 1050
        self.assertEqual(
            self.powers(self.hold.apply(self.data([100, None]))), [100, 200]
        )
        self.now = 1109.9
        self.assertEqual(
            self.powers(self.hold.apply(self.data([100, None]))), [100, 200]
        )
        self.now = 1110
        self.assertEqual(
            self.powers(self.hold.apply(self.data([100, None]))), [100, None]
        )

    def test_expired_value_stays_unknown_until_read_again(self):
        self.hold.apply(self.data([100]))
        self.now = 1010
        self.hold.apply(self.data([None]))
        self.now = 1070
        self.assertEqual(self.powers(self.hold.apply(self.data([None]))), [None])
        self.now = 1080
        self.assertEqual(self.powers(self.hold.apply(self.data([None]))), [None])
        self.now = 1090
        self.assertEqual(self.powers(self.hold.apply(self.data([150]))), [150])

    def test_good_read_restarts_the_minute(self):
        self.hold.apply(self.data([100]))
        self.now = 1050
        self.hold.apply(self.data([None]))
        self.now = 1060
        self.hold.apply(self.data([120]))
        self.now = 1100
        self.hold.apply(self.data([None]))  # new fault, 50 s after the first
        self.now = 1159
        self.assertEqual(self.powers(self.hold.apply(self.data([None]))), [120])

    def test_never_read_channel_has_nothing_to_hold(self):
        self.assertEqual(self.powers(self.hold.apply(self.data([None, 5]))), [None, 5])

    def test_channels_are_held_independently(self):
        self.hold.apply(self.data([100, 200, 300]))
        self.now = 1030
        self.hold.apply(self.data([None, 200, 300]))
        self.now = 1070
        self.hold.apply(self.data([None, None, 300]))
        self.now = 1095  # channel 1 faulted 65 s ago, channel 2 only 25 s ago
        self.assertEqual(
            self.powers(self.hold.apply(self.data([None, None, 300]))), [None, 200, 300]
        )

    def test_yields_are_held_each_on_their_own(self):
        self.hold.apply(
            self.data(
                [1, 2], days=[10, 20], hours=[5, 6], months=[30, 40], years=[50, 60]
            )
        )
        self.now = 1030
        faulty = self.data(
            [1, 2],
            days=[None, 21],
            hours=[5, None],
            months=[31, None],
            years=[None, 61],
        )
        held = self.hold.apply(faulty)
        self.assertEqual(
            held.yields,
            (api.ChannelYield(10, 5, 31, 50), api.ChannelYield(21, 6, 40, 61)),
        )
        self.now = 1090
        held = self.hold.apply(faulty)
        self.assertEqual(
            held.yields,
            (api.ChannelYield(None, 5, 31, None), api.ChannelYield(21, None, None, 61)),
        )

    def test_failed_cycle_returns_everything_held_for_a_minute(self):
        self.hold.apply(
            self.data(
                [100, 200], days=[10, 20], hours=[5, 6], months=[30, 40], years=[50, 60]
            )
        )
        for elapsed in (10, 40, 69.9):
            self.now = 1000 + elapsed
            held = self.hold.after_failure()
            if elapsed < 10 + 60:
                self.assertEqual(self.powers(held), [100, 200], elapsed)
                self.assertEqual(held.yields[1], api.ChannelYield(20, 6, 40, 60))
        self.now = 1070
        self.assertIsNone(self.hold.after_failure())

    def test_failed_cycle_with_nothing_read_yet(self):
        self.assertIsNone(self.hold.after_failure())

    def test_failed_cycle_does_not_extend_a_running_hold(self):
        self.hold.apply(self.data([100, 200]))
        self.now = 1030
        self.hold.apply(self.data([None, 200]))  # channel 0 faulted at 1030
        self.now = 1080
        self.assertEqual(self.powers(self.hold.after_failure()), [100, 200])
        self.now = 1095  # channel 0: 65 s, channel 1: 15 s
        self.assertEqual(self.powers(self.hold.after_failure()), [None, 200])

    def test_reports_which_channels_are_held(self):
        self.hold.apply(self.data([100, 200, 300], days=[1, 2, 3], hours=[4, 5, 6]))
        self.assertEqual(self.hold.held_channels, ())
        self.now = 1010
        self.hold.apply(self.data([100, None, 300], days=[1, 2, None], hours=[4, 5, 6]))
        self.assertEqual(self.hold.held_channels, (1, 2))
        self.now = 1020
        self.hold.apply(self.data([100, 210, 300], days=[1, 2, None], hours=[4, 5, 6]))
        self.assertEqual(self.hold.held_channels, (2,))
        self.now = 1071  # the hold of channel 2's day yield has run out
        self.hold.apply(self.data([100, 210, 300], days=[1, 2, None], hours=[4, 5, 6]))
        self.assertEqual(self.hold.held_channels, ())

    def test_failed_cycle_holds_every_channel_until_the_minute_is_over(self):
        self.hold.apply(self.data([100, 200]))
        self.now = 1010
        self.hold.after_failure()
        self.assertEqual(self.hold.held_channels, (0, 1))
        self.now = 1070
        self.assertIsNone(self.hold.after_failure())
        self.assertEqual(self.hold.held_channels, ())
        self.now = 1080
        self.hold.apply(self.data([100, 200]))
        self.assertEqual(self.hold.held_channels, ())


class TotalPower(unittest.TestCase):
    """The power of all channels added up."""

    def data(self, powers):
        return api.SolarCountData(
            channels=tuple(
                None if power is None else api.ChannelReading(power, 230)
                for power in powers
            )
        )

    def test_adds_up_every_channel(self):
        self.assertEqual(self.data([100, 250, -10]).total_power, 340)

    def test_unknown_if_any_channel_is_unknown(self):
        self.assertIsNone(self.data([100, None]).total_power)


class Energy(unittest.TestCase):
    """The total power of completed cycles is integrated into kWh."""

    def setUp(self):
        self.energy = api.EnergyIntegrator()

    def test_first_cycle_counts_nothing(self):
        self.energy.add(0, 1000)
        self.assertEqual(self.energy.total_kwh, 0)

    def test_constant_power_for_an_hour(self):
        for second in range(0, 3601, 10):
            self.energy.add(second, 1000)
        self.assertAlmostEqual(self.energy.total_kwh, 1.0)

    def test_trapezoidal_rule(self):
        # 0 W to 2000 W over one hour is 1 kWh, not 0 or 2.
        self.energy.add(0, 0)
        self.energy.add(3600, 2000)
        self.assertAlmostEqual(self.energy.total_kwh, 1.0)

    def test_uses_the_real_time_between_cycles(self):
        self.energy.add(0, 3600)
        self.energy.add(10, 3600)
        self.energy.add(25, 3600)
        self.assertAlmostEqual(self.energy.total_kwh, 3600 * 25 / 3_600_000)

    def test_interrupt_skips_the_gap(self):
        self.energy.add(0, 1000)
        self.energy.add(10, 1000)
        self.energy.interrupt()
        self.energy.add(1000, 1000)  # starts again here
        self.energy.add(1010, 1000)
        self.assertAlmostEqual(self.energy.total_kwh, 1000 * 20 / 3_600_000)

    def test_unknown_power_interrupts(self):
        self.energy.add(0, 1000)
        self.energy.add(10, None)
        self.energy.add(20, 1000)
        self.assertEqual(self.energy.total_kwh, 0)

    def test_negative_power_counts_as_zero(self):
        self.energy.add(0, -500)
        self.energy.add(3600, -500)
        self.assertEqual(self.energy.total_kwh, 0)
        self.energy.add(7200, 1000)
        self.assertAlmostEqual(self.energy.total_kwh, 0.5)


class Traffic(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.meter = api.TrafficMeter(window=60, clock=lambda: self.now)

    def test_average_over_the_time_so_far_then_over_the_window(self):
        self.meter.sent(100)
        self.meter.received(200)
        self.now = 110
        self.assertEqual(self.meter.rates(), (10.0, 20.0))
        self.now = 159
        self.meter.sent(20)
        self.assertEqual(self.meter.rates(), (120 / 59, 200 / 59))
        self.now = 170.5  # the first bytes are older than the window now
        self.assertEqual(self.meter.rates(), (20 / 60, 0.0))
        self.assertEqual((self.meter.bytes_sent, self.meter.bytes_received), (120, 200))

    def test_never_divides_by_less_than_a_second(self):
        self.meter.sent(50)
        self.assertEqual(self.meter.rates(), (50.0, 0.0))

    def test_reset_starts_over_from_zero(self):
        self.meter.sent(100)
        self.meter.received(200)
        self.now = 130
        self.meter.reset()
        self.assertEqual((self.meter.bytes_sent, self.meter.bytes_received), (0, 0))
        self.assertEqual(self.meter.rates(), (0.0, 0.0))
        self.meter.sent(40)
        self.now = 140  # averaged over the 10 s since the reset
        self.assertEqual(self.meter.rates(), (4.0, 0.0))


@unittest.skipUnless(hasattr(os, "ttyname"), "needs a POSIX system")
class TrafficOnTheLine(unittest.TestCase):
    def client(self, device, channels=6, **kwargs):
        client = api.SolarCountClient(
            device.port, channels, response_timeout=0.3, request_interval=0.01, **kwargs
        )
        self.addCleanup(device.close)
        self.addCleanup(client.close)
        return client

    def test_counts_every_byte_of_a_cycle(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS))
        client.read()
        stats = client.stats
        # Init and six channels: 7 requests of 10 bytes; answers of 15 + 6 x 16.
        self.assertEqual((stats.bytes_sent, stats.bytes_received), (70, 111))
        self.assertGreater(stats.send_rate, 0)
        expected = (stats.send_rate + stats.receive_rate) / api.BUS_CAPACITY * 100
        self.assertAlmostEqual(stats.bus_load, expected, delta=0.02)

    def test_yield_round_bytes(self):
        client = self.client(
            FakeSolarCount(WATTS, VOLTS), read_yields=True, yield_query_pause=0
        )
        client.read()
        stats = client.stats
        # Clock 9 + 24 yield queries of 12; answers 14 + 24 x 20.
        self.assertEqual(stats.bytes_sent, 70 + 9 + 24 * 12)
        self.assertEqual(stats.bytes_received, 111 + 14 + 24 * 20)

    def test_echo_and_stray_bytes_are_counted(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, echo=True, stray_zero=True))
        client.read()
        self.assertEqual(client.stats.bytes_received, 111 + 70 + 7)

    def test_silent_device_sends_but_receives_nothing(self):
        client = self.client(FakeSolarCount(WATTS, VOLTS, silent=True))
        with self.assertRaises(api.SolarCountTimeoutError):
            client.read()
        self.assertEqual(
            (client.stats.bytes_sent, client.stats.bytes_received), (10, 0)
        )

    def test_totals_survive_reopening_the_port(self):
        device = FakeSolarCount(WATTS, VOLTS)
        client = self.client(device, channels=1)
        client.read()
        client.close()
        client.read()
        self.assertEqual(client.stats.bytes_sent, 40)


if __name__ == "__main__":
    unittest.main()
