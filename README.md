# SunMon

SunMon is a custom Home Assistant integration that reads the power, voltage and
yields of a SolarCount solar power meter over RS485.

A SolarCount is a "vintage" multi-channel electronic power meter built to
monitor the generated power of solar panels. It was sold in Germany by the
company SolarConsult around the year 2009.

SunMon does not use the SunSpec Modbus standard for communication with the
SolarCount. It uses the SolarCount's own proprietary protocol, which is
documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).

Integration domain: `sunmon`

## What you get

For each channel of the SolarCount (up to 6):

- **power** in watts and **voltage** in volts
- the **yield** of the previous hour, day, month and year

For all channels together:

- the **sum** of the power and of each yield
- **Total power generated** in kWh, for the Energy dashboard

For troubleshooting:

- RS485 counters, throughput and bus load
- a **Values held** indicator that shows when values are being bridged after a
  fault

## Requirements

- Home Assistant 2026.9 or newer, on a Linux-based installation (Home Assistant
  OS, Container or Supervised)
- A USB to RS485 adapter, for example one with a CH343G chip, that connects a
  USB port of your Home Assistant to the RS485 interface of the SolarCount.
  Such an adapter costs under 20 €.

SunMon needs no extra Python packages; the serial port is handled with the
Python standard library.

## Wiring

Connect the adapter's A and B terminals to the SolarCount's RS485 A and B. If
the setup dialog reports that the SolarCount does not answer, the usual cause
is A and B being swapped.

## Install with HACS

1. In HACS, open the menu at the top right and choose **Custom repositories**.
2. Add `https://github.com/snbk40/SunMon` with the type **Integration**.
3. Search for **SunMon** in HACS and download it.
4. Restart Home Assistant, then continue with step 3 of the manual install.

## Install manually

1. Copy the folder `custom_components/sunmon` into the `custom_components`
   folder of your Home Assistant configuration directory (next to
   `configuration.yaml`). Create `custom_components` if it does not exist.
2. Restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration** and search for
   **SunMon**.
4. Choose the serial port of the adapter, the number of channels and the update
   interval. Everything except the serial port can be changed later with the
   **Configure** button.

The dialog tests the connection before it creates the integration. If the
serial port opens but the SolarCount does not answer, you can try again, change
the settings, or choose **Set up anyway**. The integration then shows as
*retrying setup* and keeps trying in the background; its sensors appear once
the SolarCount answers. A serial port that cannot be opened is always an
error.

To find the serial port, go to **Settings > System > Hardware > All hardware**
and search for `tty`. Prefer the `/dev/serial/by-id/...` path: it stays the same
when the adapter is replugged.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Serial port | first port found | the USB to RS485 adapter |
| Number of channels | 3 | how many SolarCount channels to read, 1 to 6, starting with the first |
| Update interval | 10 s | how often the SolarCount is read, 5 to 3600 seconds |
| Log serial traffic (TRACE) | off | write every byte on the line to the log, see [Logging](#logging) |

## What is read

| Sensor | Unit | Source in the channel response |
| --- | --- | --- |
| Channel 1 to 6 power | W | bytes 7 and 8, 16 bit signed, low byte first (-32768 to 32767) |
| Channel 1 to 6 voltage | V | byte 11 (0 to 255) |

Channel 1 in Home Assistant is channel 0 of the SolarCount.

The device is called **SolarCount**, so the entities are named for example
"SolarCount Channel 1 power" with the entity ID
`sensor.solarcount_channel_1_power`.

## When a read fails

After a fault, the last good values are kept for one minute, so a short
disturbance does not show up as a gap.

- **One channel fails** (wrong checksum, or an answer that does not fit the
  request): that channel keeps its last power and voltage, and the other
  channels keep updating. If it still cannot be read one minute after the first
  failed read, it shows as *unknown* until it can be read again.
- **The whole read fails** (the SolarCount does not answer, or the serial port
  has a problem): all values are kept and a warning is logged. If reading still
  fails after one minute, the sensors become *unavailable*.
- **Yields** are held the same way, each on its own.
- The minute counts from the first failed read, and every good read ends it.
- While any value is held, the diagnostic sensor **Values held** is on (shown as
  "Problem"), and its attribute lists the channels concerned. It goes off when
  every value is fresh again, or when the minute is over and the value has
  become unknown.
- The held value itself looks like any other; the sums use held values like
  fresh ones.
- Nothing is held right after Home Assistant starts, before the first good
  read.

## Sums over all channels

| Sensor | Unit | Adds up |
| --- | --- | --- |
| Sum of all channels power | W | the power of every configured channel |
| Sum of all channels yield previous day | none | the previous day's yield of every channel |
| Sum of all channels yield previous hour | none | the previous hour's yield of every channel |
| Sum of all channels yield previous month | none | the previous month's yield of every channel |
| Sum of all channels yield previous year | none | the previous year's yield of every channel |

- "All channels" means the number of channels set in the dialog.
- A sum is *unknown* while any of its channels is unknown: a sum over only some
  of the channels would look like a real drop. A channel that is being held
  after a fault counts with its held value.
- There is no sum of the voltages.

## Total power generated

The sensor **Total power generated** counts the energy in kWh. It is
calculated by SunMon, not read from the SolarCount: at every completed read
cycle, the sum of all channels power is integrated over the time since the
cycle before.

- Between two cycles the power is taken to change in a straight line
  (trapezoidal rule), so the result is as exact as the update interval allows.
- A failed cycle, or one in which the sum of all channels power is unknown,
  interrupts the counting: the time until the next completed cycle is not
  counted. The time while Home Assistant is not running is not counted either.
- Negative power counts as zero, so the total never goes down.
- The total is kept when Home Assistant restarts or the integration is
  reloaded.
- It starts at 0 kWh when SunMon is set up.

To use it in the Energy dashboard, go to
**Settings > Dashboards > Energy > Solar panels > Add solar production** and
choose **SolarCount Total power generated**. The first bars appear after the
next full hour.

## Yields

SunMon also reads, once a minute, for each channel:

| Sensor | Unit | What the SolarCount is asked for |
| --- | --- | --- |
| Channel 1 to 6 yield previous day | none | the day before the SolarCount's current date |
| Channel 1 to 6 yield previous hour | none | the hour before the SolarCount's current hour |
| Channel 1 to 6 yield previous month | none | the month before the SolarCount's current month |
| Channel 1 to 6 yield previous year | none | the year before the SolarCount's current year |

- The values are the **raw numbers** the SolarCount reports. Their unit is not
  known, so the sensors have none. Notes on the protocol say the hour value has
  to be divided by 60; SunMon does not do that.
- "Previous" follows the **SolarCount's own clock**, which is read first. On the
  first of a month the last day of the month before is asked for, at
  midnight hour 23, and in January month 12.
- The year query has not been seen in a real exchange yet. If your SolarCount
  does not answer it, the year sensors stay *unknown*; the other yields and the
  power readings are not affected.
- The yields are read once a minute, before the power readings, and keep their
  value in between.
- A yield whose answer has a wrong checksum, or belongs to another channel,
  keeps its last value for one minute, which covers one failed round, and then
  shows as *unknown*.
- Problems with the yields never stop the power readings. If the SolarCount does
  not answer the clock or a yield query, the round ends there, the yields not
  yet read are treated as failed, and the next round tries again. This costs up
  to 2 seconds once a minute.
- SunMon waits 300 ms before each yield query. A SolarCount was seen to
  ignore a yield query sent right after the clock answer. With six channels
  the 24 queries make a yield round take at least 7 seconds; the power
  readings follow when it is done.

## RS485 counters

Five diagnostic sensors count the traffic on the RS485 line. One count is one
request, so a healthy cycle with six channels adds 7 (the init and six
channels). The yield round adds 25 more once a minute (the clock and four
yields per channel).

| Sensor | Counts up when |
| --- | --- |
| RS485 requests | a request has been written to the serial port |
| RS485 read OK | the expected response arrived with a valid checksum |
| RS485 CRC errors | a response arrived but its checksum is wrong |
| RS485 no response errors | nothing arrived within the 2 second timeout |
| RS485 unexpected responses | a valid response arrived that does not answer the request: wrong command, wrong channel, or too short |

- Every request ends in exactly one of the four outcomes, so they add up to the
  number of requests. A gap means the serial port failed in the middle of a
  request, for example because the adapter was unplugged.
- A cycle stops at the first request without a response, so a silent SolarCount
  adds 1 request and 1 no response error per cycle.
- An unexpected answer to the init is counted, but the cycle continues.
- The counters stay available and keep counting while the SolarCount does not
  answer. They start at 0 when Home Assistant restarts or the integration is
  reloaded.
- The connection test in the setup dialog is not counted, and neither is a
  serial port that cannot be opened.
- The **Clear all diagnostics to 0** button sets the five counters, the bytes
  sent and received, both throughputs and the bus load to 0, and makes the
  error rate unknown until the next request. If a cycle is
  running, it waits for the cycle to finish first. The averages then start over
  as after a restart. Total power generated and the Values held indicator are
  not affected.

### RS485 communication error rate

One more diagnostic sensor, **RS485 communication error rate**, shows in percent
how many requests failed: CRC errors, no response errors and unexpected
responses together, as a share of all requests that ended in one of the four
outcomes.

- It covers the same time as the counters: since Home Assistant started, the
  integration was reloaded or the counters were cleared to 0.
- It is unknown until the first request has an outcome.
- Requests cut short by a failing serial port are left out, as they have no
  outcome.
- A silent SolarCount drives it towards 100 %, a healthy line keeps it at 0 %.

## RS485 throughput

Five more diagnostic sensors show how much traffic goes over the line.

| Sensor | Unit | Shows |
| --- | --- | --- |
| RS485 send throughput | B/s | bytes written, averaged over the last minute |
| RS485 receive throughput | B/s | bytes read, averaged over the last minute |
| RS485 bytes sent | B | all bytes written since start |
| RS485 bytes received | B | all bytes read since start |
| RS485 bus load | % | send and receive together, as a share of what 57600 baud can carry |

- Every byte on the serial port counts, so received bytes include echoes, stray
  zeros and garbage, not only valid answers. Bytes that are still waiting when a
  request is sent are thrown away unread and not counted.
- The bus load adds both directions because RS485 is half duplex: sending and
  receiving share one pair of wires. At 57600 baud with 8N1 the line carries
  5760 bytes per second.
- In the first minute after a start, the average is taken over the time so far.
- With six channels and an update interval of 10 seconds expect about 12 B/s
  sent, 19 B/s received and a bus load of about 0.5 %.
- Like the counters, these sensors stay available and keep updating while the
  SolarCount does not answer: sending goes on, receiving drops to zero. They
  start at 0 when Home Assistant restarts or the integration is reloaded.

## Logging

Problems are written to the Home Assistant log as warnings and errors. Turn on
debug logging for the integration to also see invalid channel responses.

To see the raw bytes on the RS485 line, switch on **Log serial traffic (TRACE)**
in the setup dialog or with the **Configure** button. Every request and every
received chunk is then logged at level `TRACE`, for example:

```
TRACE (SyncWorker_2) [custom_components.sunmon.api] ttyACM0 TX 02 b0 f0 04 00 20 00 fd 06 03
TRACE (SyncWorker_2) [custom_components.sunmon.api] ttyACM0 RX 02 f0 b0 0a 00 a0 00 87 00 00 00 ec 00 93 87 03
```

`TRACE` is one step more detailed than `DEBUG`. Home Assistant has no such level
of its own, so it cannot be set in the `logger:` configuration; the switch above
is the way to turn it on. Debug logging alone does not include the traffic.

The lines appear in the full log (**Settings > System > Logs**, raw logs or
download), not in the short list of warnings and errors. With six channels at
a 10 second interval this is about 14 lines per cycle, so switch it off again
after troubleshooting.

## Protocol

The full protocol, with frame layouts, annotated example frames, both
checksums, the timing and flow diagrams, is documented in
[docs/PROTOCOL.md](docs/PROTOCOL.md). In short:

- 57600 baud, 8 data bits, no parity, 1 stop bit
- Binary frames: `02`, destination, source, length (2 bytes, low byte first),
  command, data, CRC16 (2 bytes, low byte first), `03`
- Host address `F0`, SolarCount address `B0`
- One cycle: command `21` (init), then command `20` for each channel, with a
  0.5 second pause before each channel request
- Clock: command `04`; the answer holds year, month, day, hour and minute
- An answer carries the command of its request with the top bit set, so `20`
  is answered with `A0`
- The yields use a second, text style frame, for example `1<D00000A` for the
  yield of channel 0 on day 10, with a CRC8 instead of the CRC16

The protocol was worked out from the firmware of a data logger that reads a
SolarCount. Its source is included as
[firmware/SolarCountUART.c](firmware/SolarCountUART.c).

## Switching from Birdy Solarcount

SunMon is the same integration as Birdy Solarcount 0.14.0 under a new name. For
Home Assistant they are two different integrations (`sunmon` and
`birdy_solarcount`):

- Do not run both on the same serial port. Whichever starts second cannot open
  it.
- To switch, remove Birdy Solarcount, then set up SunMon. The history of the old
  entities and the total of **Total power generated** are not carried over.

## How it is built

| File | Purpose |
| --- | --- |
| `custom_components/sunmon/manifest.json` | Name, domain, version and metadata of the integration |
| `custom_components/sunmon/const.py` | Constants shared by all files |
| `custom_components/sunmon/api.py` | The SolarCount protocol and the serial port. No Home Assistant code |
| `custom_components/sunmon/coordinator.py` | Runs one read cycle per interval and shares the result with all sensors |
| `custom_components/sunmon/__init__.py` | Sets the integration up and tears it down |
| `custom_components/sunmon/config_flow.py` | The setup dialog and the options dialog |
| `custom_components/sunmon/entity.py` | Base class: device info and unique IDs |
| `custom_components/sunmon/sensor.py` | Power, voltage and yield sensors, their sums, the total energy, and the RS485 sensors |
| `custom_components/sunmon/binary_sensor.py` | The "Values held" indicator |
| `custom_components/sunmon/button.py` | The "Clear all diagnostics to 0" button |
| `custom_components/sunmon/translations/` | UI texts (English and German) |
| `custom_components/sunmon/brand/` | The integration's icon |
| `hacs.json` | Name and minimum Home Assistant version for HACS |
| `docs/PROTOCOL.md` | The SolarCount UART protocol |
| `firmware/SolarCountUART.c` | The original firmware source the protocol was worked out from |
| `tests/test_solarcount.py` | Tests of the protocol and the client |
| `pyproject.toml` | Settings for the code checkers ruff and mypy |

## Tests

`tests/test_solarcount.py` checks the protocol against known request and
response frames and runs the client against a simulated SolarCount on a
virtual serial port. It needs neither Home Assistant nor hardware, but it needs
Linux or macOS for the virtual serial port:

```
python3 -m unittest discover -s tests -v
```

The code is checked with [ruff](https://docs.astral.sh/ruff/) and
[mypy](https://mypy-lang.org/), configured in `pyproject.toml`:

```
ruff check .
ruff format --check .
mypy custom_components/sunmon tests
```
