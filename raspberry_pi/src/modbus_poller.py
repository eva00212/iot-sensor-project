"""
modbus_poller.py

Polls the three RS485 Modbus RTU sensors wired directly to this Raspberry
Pi's RS485 interface (device01 @ 0x01, device02 @ 0x02, device03 @ 0x03,
all on one shared bus). There is no intermediate microcontroller — the Pi
does the polling itself.

device01/device02 and device03 are the same sensor family sharing one
register table; each variant only populates the registers for the sensors
it has installed:
  device01/device02 (indoor, "CO2 variant"): registers 504-507
  device03 (outdoor):                        registers 500-515

All reads use Modbus function code 0x03 (Read Holding Registers). CRC
validation and RTU framing are handled internally by minimalmodbus;
application-level retry/timeout handling is added on top.
"""

import logging
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
MAX_RETRIES = _cfg["max_retries"]

FUNCTION_CODE = 0x03  # Read Holding Registers, per sensor manual

SLAVE_DEVICE01 = 1
SLAVE_DEVICE02 = 2
SLAVE_DEVICE03 = 3

# ── Register map (confirmed from sensor manual) ───────────────────────────────
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

# Indoor block: 504..507 (humidity, temperature, [unused 506], co2)
INDOOR_REG_START = REG_HUMIDITY
INDOOR_REG_COUNT = REG_CO2 - REG_HUMIDITY + 1
INDOOR_OFF_HUMID = REG_HUMIDITY - INDOOR_REG_START
INDOOR_OFF_TEMP  = REG_TEMPERATURE - INDOOR_REG_START
INDOOR_OFF_CO2   = REG_CO2 - INDOOR_REG_START

# Outdoor block: 500..515 (wind, ...unused..., humidity, temperature,
# ...unused..., rainfall, ...unused..., solar) — one block read is simpler
# and cheaper than 5 separate requests.
OUTDOOR_REG_START = REG_WIND_SPEED
OUTDOOR_REG_COUNT = REG_SOLAR - REG_WIND_SPEED + 1
OUTDOOR_OFF_WIND  = REG_WIND_SPEED - OUTDOOR_REG_START
OUTDOOR_OFF_HUMID = REG_HUMIDITY - OUTDOOR_REG_START
OUTDOOR_OFF_TEMP  = REG_TEMPERATURE - OUTDOOR_REG_START
OUTDOOR_OFF_RAIN  = REG_RAINFALL - OUTDOOR_REG_START
OUTDOOR_OFF_SOLAR = REG_SOLAR - OUTDOOR_REG_START

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


def _read_block(slave_addr: int, start_reg: int, count: int) -> list | None:
    """
    Reads `count` holding registers starting at `start_reg` from
    `slave_addr`, with retry/timeout handling. CRC validation and framing
    are handled internally by minimalmodbus. Returns the raw unsigned
    register values, or None if all attempts failed.
    """
    instrument = _get_instrument(slave_addr)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return instrument.read_registers(start_reg, count, functioncode=FUNCTION_CODE)
        except (minimalmodbus.ModbusException, serial.SerialException, OSError) as e:
            logger.warning(
                "[Modbus] slave 0x%02X attempt %d/%d: %s",
                slave_addr, attempt, MAX_RETRIES, e,
            )

    logger.error("[Modbus] slave 0x%02X: failed after %d attempts", slave_addr, MAX_RETRIES)
    return None


def _to_signed16(value: int) -> int:
    """Convert a raw unsigned 16-bit register value to signed (two's complement)."""
    return value - 0x10000 if value >= 0x8000 else value


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── Public API ────────────────────────────────────────────────────────────────
def poll_indoor(site_id: str, device_id: str, slave_addr: int) -> dict:
    """Polls an indoor sensor (device01/device02); returns a payload dict
    ready for data_validator.validate()."""
    regs = _read_block(slave_addr, INDOOR_REG_START, INDOOR_REG_COUNT)

    payload = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": _now_iso(),
    }

    if regs is None:
        payload["temperature"]  = 0.0
        payload["humidity"]     = 0.0
        payload["device_fault"] = "true"
        return payload

    payload["temperature"]  = round(_to_signed16(regs[INDOOR_OFF_TEMP]) * TEMPERATURE_SCALE, 1)
    payload["humidity"]     = round(regs[INDOOR_OFF_HUMID] * HUMIDITY_SCALE, 1)
    payload["co2"]          = regs[INDOOR_OFF_CO2]
    payload["device_fault"] = "false"
    return payload


def poll_outdoor(site_id: str, device_id: str, slave_addr: int) -> dict:
    """Polls the outdoor sensor (device03); returns a payload dict ready
    for data_validator.validate()."""
    regs = _read_block(slave_addr, OUTDOOR_REG_START, OUTDOOR_REG_COUNT)

    payload = {
        "site_id":   site_id,
        "device_id": device_id,
        "timestamp": _now_iso(),
    }

    if regs is None:
        payload["temperature"]   = 0.0
        payload["humidity"]      = 0.0
        payload["rain_detected"] = "false"
        payload["device_fault"]  = "true"
        return payload

    payload["temperature"] = round(_to_signed16(regs[OUTDOOR_OFF_TEMP]) * TEMPERATURE_SCALE, 1)
    payload["humidity"]    = round(regs[OUTDOOR_OFF_HUMID] * HUMIDITY_SCALE, 1)
    payload["wind_speed"]  = round(regs[OUTDOOR_OFF_WIND] * WIND_SPEED_SCALE, 1)

    # Register 513 reports a rainfall amount (mm), not a direct boolean
    # flag. rain_detected is derived: any nonzero rainfall this interval.
    rainfall = regs[OUTDOOR_OFF_RAIN] * RAINFALL_SCALE
    payload["rain_detected"] = "true" if rainfall > 0.0 else "false"

    payload["solar_radiation"] = round(regs[OUTDOOR_OFF_SOLAR] * SOLAR_SCALE, 1)
    payload["device_fault"]    = "false"
    return payload


def poll_device01(site_id: str) -> dict:
    return poll_indoor(site_id, "device01", SLAVE_DEVICE01)


def poll_device02(site_id: str) -> dict:
    return poll_indoor(site_id, "device02", SLAVE_DEVICE02)


def poll_device03(site_id: str) -> dict:
    return poll_outdoor(site_id, "device03", SLAVE_DEVICE03)
