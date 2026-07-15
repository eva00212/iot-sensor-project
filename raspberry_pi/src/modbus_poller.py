"""
modbus_poller.py

Polls the three RS485 Modbus RTU sensors wired directly to this Raspberry
Pi's RS485 interface (device01 @ 0x01, device02 @ 0x02, device03 @ 0x03,
all on one shared bus). There is no intermediate microcontroller — the Pi
does the polling itself.

The low-level Modbus RTU communication below (frame building, CRC16,
serial port setup, buffer flushing, single 500-515 block read) is a direct
port of rs485_weather_station.py's WeatherStationSensor, which was
independently verified against this exact hardware. An earlier version of
this file used the `minimalmodbus` library instead, with several narrow,
separate register reads per device (e.g. 500, then 504-505, then 507,
513, 515) rather than one full-block read -- every request timed out on
real hardware with that approach, even though the raw-pyserial
single-block-read approach here works. Do not reintroduce minimalmodbus or
split this back into narrow reads without re-verifying against real
hardware first.

Sensor: small all-in-one ultrasonic weather station (manual:
SN-*-FSXCS-N01) that exposes many measurements in one continuous register
table (500-515); each physical unit only has the sensors installed that
are relevant to its deployment:
  device01/device02 (indoor, "CO2 variant"): humidity/temperature/CO2
  device03 (outdoor): wind speed/humidity/temperature/rainfall/solar
Every variant shares the same 16-register table, so every device is
polled with the exact same single block read (function code 0x03,
registers 500-515) -- the payload builder then extracts only the fields
relevant to that device's variant.

Retry logic (modbus_retry_count attempts, modbus_retry_delay_seconds
apart, both configurable and independent of the poll interval) wraps this
block read. This is a resilience feature for the always-on collector
service on top of the verified communication layer -- the reference
rs485_weather_station.py script itself has no retries and simply reports
a failed read on its next scheduled poll. Exhausting all retries is
reported to the caller as (None, error_message), never raised: giving up
on one reading must never stop the collector.
"""

import logging
import struct
import time
from datetime import datetime
from pathlib import Path

import serial
import yaml

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent.parent / "config" / "modbus_config.yaml"

def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)

_cfg = _load_config()

SERIAL_PORT = _cfg["serial_port"]
BAUDRATE    = _cfg["baudrate"]
BYTESIZE    = _cfg["bytesize"]           # matches pyserial's *BITS constants (e.g. 8 == serial.EIGHTBITS)
PARITY      = _cfg["parity"]             # 'N', 'E', or 'O' — matches pyserial's PARITY_* constants
STOPBITS    = _cfg["stopbits"]           # matches pyserial's STOPBITS_* constants (e.g. 1 == serial.STOPBITS_ONE)
TIMEOUT_SEC = _cfg["timeout_seconds"]

MAX_RETRIES     = _cfg["modbus_retry_count"]
RETRY_DELAY_SEC = _cfg["modbus_retry_delay_seconds"]

READ_HOLDING_REGISTERS = 0x03  # the only function code this sensor supports, per its manual

SLAVE_DEVICE01 = 1
SLAVE_DEVICE02 = 2
SLAVE_DEVICE03 = 3

# ── Register map: one full block read (500-515) per poll, matching the
# verified rs485_weather_station.py exactly ────────────────────────────────────
FIRST_REGISTER = 500
REGISTER_COUNT = 16  # covers 500..515 inclusive

REG_WIND_SPEED  = 500  # raw × 0.1 m/s
REG_HUMIDITY    = 504  # raw × 0.1 %RH
REG_TEMPERATURE = 505  # signed 16-bit (two's complement), raw × 0.1 °C
REG_CO2         = 507  # raw integer, ppm (CO2 variant only)
REG_RAINFALL    = 513  # raw × 0.1 mm (internal only — derives rain_detected)
REG_SOLAR       = 515  # raw value, W/m²

WIND_SPEED_SCALE  = 0.1
HUMIDITY_SCALE    = 0.1
TEMPERATURE_SCALE = 0.1
RAINFALL_SCALE    = 0.1
SOLAR_SCALE       = 1.0


class ModbusError(Exception):
    """Any Modbus RTU communication/framing failure (no/short response,
    slave exception, unexpected header, CRC mismatch). Mirrors
    rs485_weather_station.py's ModbusError."""


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _build_request(slave_addr: int, start_addr: int, quantity: int) -> bytes:
    body = struct.pack(">BBHH", slave_addr, READ_HOLDING_REGISTERS, start_addr, quantity)
    crc = _crc16_modbus(body)
    # CRC is sent low byte first, high byte second (per datasheet 4.2/4.4)
    return body + struct.pack("<H", crc)


# ── Serial port: one shared connection for the whole RS485 bus ────────────────
# All three sensors share one physical bus/UART, so (like
# rs485_weather_station.py's single self.ser) there is exactly one
# serial.Serial instance here, not one per slave address.
_ser: "serial.Serial | None" = None


def _get_serial() -> "serial.Serial":
    """Opens the shared serial port exactly once, with every setting
    passed atomically to the serial.Serial() constructor -- matching
    rs485_weather_station.py's WeatherStationSensor.__init__ exactly,
    rather than opening with library defaults and reconfiguring
    properties one at a time afterward."""
    global _ser
    if _ser is not None and _ser.is_open:
        return _ser

    _ser = serial.Serial(
        port=SERIAL_PORT,
        baudrate=BAUDRATE,
        bytesize=BYTESIZE,
        parity=PARITY,
        stopbits=STOPBITS,
        timeout=TIMEOUT_SEC,
    )
    return _ser


def _read_registers_once(slave_addr: int, start_addr: int, quantity: int) -> list[int]:
    """Single Modbus RTU transaction, no retries -- raises ModbusError on
    any failure. Direct port of
    rs485_weather_station.WeatherStationSensor.read_registers()."""
    ser = _get_serial()
    request = _build_request(slave_addr, start_addr, quantity)

    ser.reset_input_buffer()
    ser.write(request)
    ser.flush()  # block until bytes are physically out, same as the verified script

    expected_len = 3 + 2 * quantity + 2  # addr+func+bytecount+data+crc
    response = ser.read(expected_len)

    if len(response) < 5:
        raise ModbusError(
            f"No/short response from slave {slave_addr:#04x} "
            f"({len(response)} bytes) -- check wiring/address/baud rate"
        )

    addr, func = response[0], response[1]

    if func & 0x80:
        raise ModbusError(
            f"Slave {addr:#04x} returned exception code {response[2]:#04x} "
            f"for function {func & 0x7F:#04x}"
        )

    if addr != slave_addr or func != READ_HOLDING_REGISTERS:
        raise ModbusError(f"Unexpected response header: {response.hex()}")

    byte_count = response[2]
    if len(response) != 3 + byte_count + 2:
        raise ModbusError(f"Incomplete frame: {response.hex()}")

    payload, recv_crc = response[:-2], response[-2:]
    calc_crc = _crc16_modbus(payload)
    recv_crc_val = recv_crc[0] | (recv_crc[1] << 8)
    if calc_crc != recv_crc_val:
        raise ModbusError(
            f"CRC mismatch: calculated {calc_crc:#06x}, "
            f"received {recv_crc_val:#06x}, frame {response.hex()}"
        )

    data = payload[3:]
    return [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data), 2)]


def _read_block(slave_addr: int) -> tuple[list[int] | None, str | None]:
    """
    Reads the full 16-register block (500-515) from `slave_addr`, retrying
    up to MAX_RETRIES times with RETRY_DELAY_SEC between attempts. Returns
    (registers, None) on success, or (None, error_message) if every
    attempt failed -- this function never raises for a communication
    failure, only for a programming error.

    Serial port acquisition (which opens the port on first use) is inside
    the retry loop, not just the read itself: right after boot the UART
    device node can briefly not exist yet, or a USB-RS485 adapter can be
    transiently unavailable, and that failure must be retried exactly like
    any other transient Modbus error rather than raising out of the poll
    loop.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            values = _read_registers_once(slave_addr, FIRST_REGISTER, REGISTER_COUNT)
            return values, None
        except (ModbusError, serial.SerialException, OSError) as e:
            last_error = e
            logger.warning(
                "[Modbus] slave 0x%02X attempt %d/%d: %s",
                slave_addr, attempt, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)

    message = f"slave 0x{slave_addr:02X}: {type(last_error).__name__}: {last_error}"
    logger.error("[Modbus] %s (failed after %d attempts)", message, MAX_RETRIES)
    return None, message


def _to_signed16(value: int) -> int:
    """Convert a raw unsigned 16-bit register value to signed (two's complement)."""
    return value - 0x10000 if value >= 0x8000 else value


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _extract(regs: list[int], reg_addr: int) -> int:
    return regs[reg_addr - FIRST_REGISTER]


# ── Public API ────────────────────────────────────────────────────────────────
def poll_indoor(site_id: str, device_id: str, slave_addr: int) -> dict:
    """Polls an indoor sensor (device01/device02); returns a payload dict
    ready for data_validator.validate(). One block read of the full
    500-515 register range -- all fields come from the same transaction,
    so a failure means the whole reading falls back, not per-field."""
    regs, error = _read_block(slave_addr)

    payload = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": _now_iso(),
    }

    if regs is not None:
        payload["temperature"]  = round(_to_signed16(_extract(regs, REG_TEMPERATURE)) * TEMPERATURE_SCALE, 1)
        payload["humidity"]     = round(_extract(regs, REG_HUMIDITY) * HUMIDITY_SCALE, 1)
        payload["co2"]          = _extract(regs, REG_CO2)
        payload["device_fault"] = "false"
    else:
        payload["temperature"]   = 0.0
        payload["humidity"]      = 0.0
        payload["device_fault"]  = "true"
        payload["error_message"] = error

    return payload


def poll_outdoor(site_id: str, device_id: str, slave_addr: int) -> dict:
    """Polls the outdoor sensor (device03); returns a payload dict ready
    for data_validator.validate(). One block read of the full 500-515
    register range -- all fields come from the same transaction, so a
    failure means the whole reading falls back, not per-field."""
    regs, error = _read_block(slave_addr)

    payload = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": _now_iso(),
    }

    if regs is not None:
        payload["temperature"] = round(_to_signed16(_extract(regs, REG_TEMPERATURE)) * TEMPERATURE_SCALE, 1)
        payload["humidity"]    = round(_extract(regs, REG_HUMIDITY) * HUMIDITY_SCALE, 1)
        payload["wind_speed"]  = round(_extract(regs, REG_WIND_SPEED) * WIND_SPEED_SCALE, 1)

        # Register 513 reports a rainfall amount (mm), not a direct
        # boolean flag. rain_detected is derived: any nonzero rainfall
        # this interval.
        rainfall = _extract(regs, REG_RAINFALL) * RAINFALL_SCALE
        payload["rain_detected"] = "true" if rainfall > 0.0 else "false"

        payload["solar_radiation"] = round(_extract(regs, REG_SOLAR) * SOLAR_SCALE, 1)
        payload["device_fault"]    = "false"
    else:
        payload["temperature"]   = 0.0
        payload["humidity"]      = 0.0
        # rain_detected is a required field, so it always gets a value --
        # "false" is the safe fallback when the read failed.
        payload["rain_detected"] = "false"
        payload["device_fault"]  = "true"
        payload["error_message"] = error

    return payload


def poll_device01(site_id: str) -> dict:
    return poll_indoor(site_id, "device01", SLAVE_DEVICE01)


def poll_device02(site_id: str) -> dict:
    return poll_indoor(site_id, "device02", SLAVE_DEVICE02)


def poll_device03(site_id: str) -> dict:
    return poll_outdoor(site_id, "device03", SLAVE_DEVICE03)
