"""
modbus_poller.py

Polls the three RS485 Modbus RTU sensors wired directly to this Raspberry
Pi's RS485 interface (device01 @ 0x01, device02 @ 0x02, device03 @ 0x03,
all on one shared bus). There is no intermediate microcontroller — the Pi
does the polling itself.

The sensor is a small all-in-one ultrasonic weather station (manual:
SN-*-FSXCS-N01) that exposes many measurements in one continuous register
table (500-515); each physical unit only has the sensors installed that
are relevant to its deployment:
  device01/device02 (indoor, "CO2 variant"): humidity/temperature/CO2
  device03 (outdoor): wind speed/humidity/temperature/rainfall/solar

All reads use Modbus function code 0x03 (Read Holding Registers). Each
read is deliberately narrow — matching exactly the register spans the
manual's own worked examples demonstrate (single registers, or the
humidity+temperature pair) — rather than one wide block read spanning
unused registers, since the manual never demonstrates anything wider than
a 2-register read. CRC validation and RTU framing are handled internally
by minimalmodbus; application-level retry/timeout handling is added on
top, with >=200ms spacing between successive reads to the same device per
the manual's FAQ ("host polling interval and response wait time must both
be set to at least 200ms").
"""

import logging
import time
from datetime import datetime
from pathlib import Path

import minimalmodbus
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
BYTESIZE    = _cfg["bytesize"]
PARITY      = _cfg["parity"]        # 'N', 'E', or 'O' — matches pyserial's PARITY_* constants
STOPBITS    = _cfg["stopbits"]
TIMEOUT_SEC = _cfg["timeout_seconds"]

# Retry behavior for a single register-block read. Independent of the poll
# interval (collector.py's POLL_INTERVAL_SEC) by design -- this is the same
# whether polling every 10s or every 600s. Exhausting all retries is
# reported to the caller as (None, error_message), never raised: giving up
# on one reading must never stop the collector.
MAX_RETRIES      = _cfg["modbus_retry_count"]
RETRY_DELAY_SEC  = _cfg["modbus_retry_delay_seconds"]

# Minimum spacing between successive reads to the same device, per the
# manual's FAQ requirement (>=200ms). Reuses the same config value
# collector.py uses for spacing between *different* devices.
INTER_READ_DELAY_SEC = _cfg["inter_poll_delay_ms"] / 1000

FUNCTION_CODE = 0x03  # Read Holding Registers, per sensor manual

SLAVE_DEVICE01 = 1
SLAVE_DEVICE02 = 2
SLAVE_DEVICE03 = 3

# ── Register map (confirmed from sensor manual, worked examples verified) ─────
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

# Humidity+temperature are read together (they're adjacent, and this exact
# pair-read is the manual's own worked example in section 4.4.3). Every
# other value is its own single-register read — each individually matches
# a manual worked example (wind speed) or stays a minimal, isolated read
# rather than sweeping through unrelated registers.
HUMID_TEMP_REG_START = REG_HUMIDITY
HUMID_TEMP_REG_COUNT = REG_TEMPERATURE - REG_HUMIDITY + 1  # 2
HT_OFF_HUMID = REG_HUMIDITY - HUMID_TEMP_REG_START     # 0
HT_OFF_TEMP  = REG_TEMPERATURE - HUMID_TEMP_REG_START  # 1

# ── Instruments (one per slave address) ────────────────────────────────────────
# minimalmodbus shares one underlying serial connection across all
# Instrument objects opened on the same port string, so creating one
# instrument per slave address is the correct way to poll several devices
# on one shared RS485 bus.
_instruments: dict[int, "minimalmodbus.Instrument"] = {}


def _get_instrument(slave_addr: int) -> "minimalmodbus.Instrument":
    inst = _instruments.get(slave_addr)
    if inst is not None:
        return inst

    inst = minimalmodbus.Instrument(SERIAL_PORT, slave_addr, mode=minimalmodbus.MODE_RTU)
    inst.serial.baudrate = BAUDRATE
    inst.serial.bytesize = BYTESIZE
    inst.serial.parity   = PARITY
    inst.serial.stopbits = STOPBITS
    inst.serial.timeout  = TIMEOUT_SEC
    inst.close_port_after_each_call = False

    # The attached RS485 board auto-switches transmit/receive direction, so
    # no manual DE/RE GPIO toggling is needed. If a future board requires
    # it, pyserial's kernel-level RS485 mode is the robust way to add it:
    #   import serial.rs485
    #   inst.serial.rs485_mode = serial.rs485.RS485Settings()

    _instruments[slave_addr] = inst
    return inst


def _read_block(slave_addr: int, start_reg: int, count: int) -> tuple[list | None, str | None]:
    """
    Reads `count` holding registers starting at `start_reg` from
    `slave_addr`, retrying up to MAX_RETRIES times with RETRY_DELAY_SEC
    between attempts. CRC validation and framing are handled internally by
    minimalmodbus. Returns (values, None) on success, or
    (None, error_message) if every attempt failed -- this function never
    raises for a communication failure, only for a programming error.

    Instrument acquisition (which opens the serial port on first use) is
    inside the retry loop, not just the read itself: right after boot the
    UART device node can briefly not exist yet, or a USB-RS485 adapter can
    be transiently unavailable, and that failure must be retried exactly
    like any other transient Modbus error rather than raising out of the
    poll loop.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            instrument = _get_instrument(slave_addr)
            values = instrument.read_registers(start_reg, count, functioncode=FUNCTION_CODE)
            return values, None
        except (minimalmodbus.ModbusException, serial.SerialException, OSError) as e:
            last_error = e
            logger.warning(
                "[Modbus] slave 0x%02X reg %d attempt %d/%d: %s",
                slave_addr, start_reg, attempt, MAX_RETRIES, e,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)

    message = f"slave 0x{slave_addr:02X} reg {start_reg}: {type(last_error).__name__}: {last_error}"
    logger.error("[Modbus] %s (failed after %d attempts)", message, MAX_RETRIES)
    return None, message


def _to_signed16(value: int) -> int:
    """Convert a raw unsigned 16-bit register value to signed (two's complement)."""
    return value - 0x10000 if value >= 0x8000 else value


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── Public API ────────────────────────────────────────────────────────────────
def poll_indoor(site_id: str, device_id: str, slave_addr: int) -> dict:
    """Polls an indoor sensor (device01/device02); returns a payload dict
    ready for data_validator.validate().

    Two separate reads, each matching the manual's proven pattern:
      1. registers 504-505 (humidity, temperature) — the manual's own
         worked example (section 4.4.3)
      2. register 507 (CO2) — single-register read
    """
    ht_regs, ht_err = _read_block(slave_addr, HUMID_TEMP_REG_START, HUMID_TEMP_REG_COUNT)
    time.sleep(INTER_READ_DELAY_SEC)
    co2_regs, co2_err = _read_block(slave_addr, REG_CO2, 1)

    payload = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": _now_iso(),
    }

    if ht_regs is not None:
        payload["temperature"] = round(_to_signed16(ht_regs[HT_OFF_TEMP]) * TEMPERATURE_SCALE, 1)
        payload["humidity"]    = round(ht_regs[HT_OFF_HUMID] * HUMIDITY_SCALE, 1)
    else:
        payload["temperature"] = 0.0
        payload["humidity"]    = 0.0

    if co2_regs is not None:
        payload["co2"] = co2_regs[0]

    errors = [e for e in (ht_err, co2_err) if e]
    payload["device_fault"] = "true" if errors else "false"
    if errors:
        payload["error_message"] = "; ".join(errors)
    return payload


def poll_outdoor(site_id: str, device_id: str, slave_addr: int) -> dict:
    """Polls the outdoor sensor (device03); returns a payload dict ready
    for data_validator.validate().

    Four separate reads, each matching the manual's proven pattern or a
    minimal single-register read:
      1. register 500 (wind speed) — the manual's own worked example
         (section 4.4.1)
      2. registers 504-505 (humidity, temperature) — the manual's own
         worked example (section 4.4.3)
      3. register 513 (rainfall)
      4. register 515 (solar radiation)
    """
    wind_regs, wind_err   = _read_block(slave_addr, REG_WIND_SPEED, 1)
    time.sleep(INTER_READ_DELAY_SEC)
    ht_regs, ht_err       = _read_block(slave_addr, HUMID_TEMP_REG_START, HUMID_TEMP_REG_COUNT)
    time.sleep(INTER_READ_DELAY_SEC)
    rain_regs, rain_err   = _read_block(slave_addr, REG_RAINFALL, 1)
    time.sleep(INTER_READ_DELAY_SEC)
    solar_regs, solar_err = _read_block(slave_addr, REG_SOLAR, 1)

    payload = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": _now_iso(),
    }

    if ht_regs is not None:
        payload["temperature"] = round(_to_signed16(ht_regs[HT_OFF_TEMP]) * TEMPERATURE_SCALE, 1)
        payload["humidity"]    = round(ht_regs[HT_OFF_HUMID] * HUMIDITY_SCALE, 1)
    else:
        payload["temperature"] = 0.0
        payload["humidity"]    = 0.0

    if wind_regs is not None:
        payload["wind_speed"] = round(wind_regs[0] * WIND_SPEED_SCALE, 1)

    # Register 513 reports a rainfall amount (mm), not a direct boolean
    # flag. rain_detected is derived: any nonzero rainfall this interval.
    # rain_detected is a required field, so it always gets a value —
    # "false" is the safe fallback if this particular read failed.
    if rain_regs is not None:
        rainfall = rain_regs[0] * RAINFALL_SCALE
        payload["rain_detected"] = "true" if rainfall > 0.0 else "false"
    else:
        payload["rain_detected"] = "false"

    if solar_regs is not None:
        payload["solar_radiation"] = round(solar_regs[0] * SOLAR_SCALE, 1)

    errors = [e for e in (wind_err, ht_err, rain_err, solar_err) if e]
    payload["device_fault"] = "true" if errors else "false"
    if errors:
        payload["error_message"] = "; ".join(errors)
    return payload


def poll_device01(site_id: str) -> dict:
    return poll_indoor(site_id, "device01", SLAVE_DEVICE01)


def poll_device02(site_id: str) -> dict:
    return poll_indoor(site_id, "device02", SLAVE_DEVICE02)


def poll_device03(site_id: str) -> dict:
    return poll_outdoor(site_id, "device03", SLAVE_DEVICE03)
