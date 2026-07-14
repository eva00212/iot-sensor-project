"""
RS485 / Modbus-RTU driver for the SN-3003-FSXCS-N01 small ultrasonic
all-in-one weather station, read over a Raspberry Pi 3 hardware UART.

Wiring (per the supplied PCB schematic):
  RPi pin  8 (GPIO14 / TXD) -> transceiver RXD_SENSOR (drives the bus)
  RPi pin 10 (GPIO15 / RXD) <- transceiver TXD_SENSOR (reads the bus)
  RPi pins 2/4 (5V)         -> transceiver supply, filtered by L2 (120 ohm
                                ferrite bead) + C6 (100nF decoupling cap)
  RPi pins 14/20/25/30/34/39 -> GND

No RPi GPIO is routed to a DE/RE direction-control pin on the header, so
the transceiver on this PCB is assumed to be an auto-direction-sensing
RS485 chip (e.g. MAX13487E / SP485EN-type) that needs no software-driven
half-duplex switching -- the Pi just does plain UART TX/RX. If your board
actually uses a manually-directed transceiver (e.g. a bare MAX485 without
auto-flow-control), wire its DE/RE to a spare GPIO and pass de_re_pin=.

Sensor communication parameters (from the datasheet, section 4.1):
  8 data bits, no parity, 1 stop bit, CRC16 (Modbus, low byte first)
  Baud rate: 2400 / 4800 / 9600, factory default 4800
  Default slave address: 0x01
  Only function code 0x03 (Read Holding Registers) is supported.

Raspberry Pi 5 setup (required once, before running this script):
  Unlike the Pi 3/4, GPIO and UART on the Pi 5 are handled by the RP1
  I/O controller, and onboard Bluetooth is wired to its own dedicated
  RP1 UART -- it does NOT share GPIO14/15, so there is no need for the
  old `dtoverlay=disable-bt` trick.
  1. `sudo raspi-config` -> Interface Options -> Serial Port ->
        "login shell over serial" = No, "serial port hardware" = Yes
     This edits /boot/firmware/config.txt for you (note: /boot/firmware,
     not /boot, on current Raspberry Pi OS) and enables uart0 on
     GPIO14/15 (physical pins 8/10).
  2. Reboot, then confirm the alias: `ls -l /dev/serial0`.
     Use "/dev/serial0" (not a hardcoded "/dev/ttyAMA0") as the port --
     on the Pi 5, the RP1 UART for GPIO14/15 has shown up under varying
     internal names (e.g. ttyAMA0 vs ttyAMA10) across kernel versions;
     /dev/serial0 is the stable symlink raspi-config points at the
     correct device regardless.
"""

from __future__ import annotations

import argparse
import struct
import time
from dataclasses import dataclass
from typing import Optional

import serial

READ_HOLDING_REGISTERS = 0x03

# name -> (register_address, scale_divisor, signed)
# Addresses match the datasheet's raw register column (e.g. 500), which is
# what goes on the wire -- NOT the PLC/"40501-style" column.
REGISTER_MAP = {
    "wind_speed_mps": (500, 10, False),
    "wind_force": (501, 1, False),
    "wind_dir_octant": (502, 1, False),      # 0=N, 2=E, clockwise, 0-7
    "wind_dir_deg": (503, 1, False),          # 0=N, 90=E, clockwise, 0-360
    "humidity_pct": (504, 10, False),
    "temperature_c": (505, 10, True),         # two's complement below 0C
    "noise_db": (506, 10, False),
    "pm2_5_or_co2": (507, 1, False),          # CO2 (ppm) on CO2-variant units
    "pm10": (508, 1, False),
    "pressure_kpa": (509, 10, False),
    "lux_high16": (510, 1, False),            # combine with lux_low16
    "lux_low16": (511, 1, False),
    "lux_hundreds": (512, 1, False),
    "rain_mm": (513, 10, False),
    "compass_deg": (514, 100, False),
    "solar_radiation_wm2": (515, 1, False),
}

FIRST_REGISTER = 500
REGISTER_COUNT = 16  # covers 500..515 inclusive


class ModbusError(Exception):
    pass


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _self_test_crc() -> None:
    # Verifies our CRC matches the datasheet's worked example (4.4.1):
    # query for wind speed at slave 0x01, start 0x01F4, qty 1
    frame = bytes([0x01, 0x03, 0x01, 0xF4, 0x00, 0x01])
    crc = crc16_modbus(frame)
    assert crc & 0xFF == 0xC4 and (crc >> 8) & 0xFF == 0x04, (
        f"CRC self-test failed: got {crc:#06x}, expected 0x04C4"
    )


_self_test_crc()


@dataclass
class WeatherReading:
    wind_speed_mps: float
    wind_force: int
    wind_dir_octant: int
    wind_dir_deg: float
    humidity_pct: float
    temperature_c: float
    noise_db: float
    pm2_5_or_co2: int
    pm10: int
    pressure_kpa: float
    illuminance_lux: int
    lux_hundreds: int
    rain_mm: float
    compass_deg: float
    solar_radiation_wm2: int


class WeatherStationSensor:
    def __init__(
        self,
        port: str = "/dev/serial0",
        baudrate: int = 4800,
        slave_addr: int = 0x01,
        timeout: float = 1.0,
        de_re_pin: Optional[int] = None,
    ):
        self.slave_addr = slave_addr
        self.de_re_pin = de_re_pin
        self._gpio = None

        if de_re_pin is not None:
            import RPi.GPIO as GPIO  # imported lazily; only needed if used

            self._gpio = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(de_re_pin, GPIO.OUT, initial=GPIO.LOW)  # receive mode

        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )

    def close(self) -> None:
        self.ser.close()
        if self._gpio is not None:
            self._gpio.cleanup(self.de_re_pin)

    def __enter__(self) -> "WeatherStationSensor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _build_request(self, start_addr: int, quantity: int) -> bytes:
        body = struct.pack(
            ">BBHH", self.slave_addr, READ_HOLDING_REGISTERS, start_addr, quantity
        )
        crc = crc16_modbus(body)
        # CRC is sent low byte first, high byte second (per datasheet 4.2/4.4)
        return body + struct.pack("<H", crc)

    def _set_driver_enable(self, enabled: bool) -> None:
        if self._gpio is not None:
            self._gpio.output(self.de_re_pin, self._gpio.HIGH if enabled else self._gpio.LOW)

    def read_registers(self, start_addr: int, quantity: int) -> list[int]:
        """Read `quantity` holding registers starting at `start_addr`.
        Returns a list of raw unsigned 16-bit register values."""
        request = self._build_request(start_addr, quantity)

        self.ser.reset_input_buffer()
        self._set_driver_enable(True)
        try:
            self.ser.write(request)
            self.ser.flush()  # block until bytes are physically out
        finally:
            self._set_driver_enable(False)

        expected_len = 3 + 2 * quantity + 2  # addr+func+bytecount+data+crc
        response = self.ser.read(expected_len)

        if len(response) < 5:
            raise ModbusError(
                f"No/short response from slave {self.slave_addr:#04x} "
                f"({len(response)} bytes) -- check wiring/address/baud rate"
            )

        addr, func = response[0], response[1]

        if func & 0x80:
            raise ModbusError(
                f"Slave {addr:#04x} returned exception code {response[2]:#04x} "
                f"for function {func & 0x7F:#04x}"
            )

        if addr != self.slave_addr or func != READ_HOLDING_REGISTERS:
            raise ModbusError(f"Unexpected response header: {response.hex()}")

        byte_count = response[2]
        if len(response) != 3 + byte_count + 2:
            raise ModbusError(f"Incomplete frame: {response.hex()}")

        payload, recv_crc = response[:-2], response[-2:]
        calc_crc = crc16_modbus(payload)
        recv_crc_val = recv_crc[0] | (recv_crc[1] << 8)
        if calc_crc != recv_crc_val:
            raise ModbusError(
                f"CRC mismatch: calculated {calc_crc:#06x}, "
                f"received {recv_crc_val:#06x}, frame {response.hex()}"
            )

        data = payload[3:]
        return [
            struct.unpack(">H", data[i : i + 2])[0] for i in range(0, len(data), 2)
        ]

    @staticmethod
    def _to_signed16(raw: int) -> int:
        return raw - 0x10000 if raw >= 0x8000 else raw

    def read_all(self) -> WeatherReading:
        regs = self.read_registers(FIRST_REGISTER, REGISTER_COUNT)
        values = {}
        for name, (addr, scale, signed) in REGISTER_MAP.items():
            raw = regs[addr - FIRST_REGISTER]
            if signed:
                raw = self._to_signed16(raw)
            values[name] = raw / scale if scale != 1 else raw

        illuminance_lux = (int(values["lux_high16"]) << 16) | int(values["lux_low16"])

        return WeatherReading(
            wind_speed_mps=values["wind_speed_mps"],
            wind_force=int(values["wind_force"]),
            wind_dir_octant=int(values["wind_dir_octant"]),
            wind_dir_deg=values["wind_dir_deg"],
            humidity_pct=values["humidity_pct"],
            temperature_c=values["temperature_c"],
            noise_db=values["noise_db"],
            pm2_5_or_co2=int(values["pm2_5_or_co2"]),
            pm10=int(values["pm10"]),
            pressure_kpa=values["pressure_kpa"],
            illuminance_lux=illuminance_lux,
            lux_hundreds=int(values["lux_hundreds"]),
            rain_mm=values["rain_mm"],
            compass_deg=values["compass_deg"],
            solar_radiation_wm2=int(values["solar_radiation_wm2"]),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll the RS485 weather station")
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--baud", type=int, default=4800)
    parser.add_argument("--address", type=lambda x: int(x, 0), default=0x01)
    parser.add_argument("--de-re-pin", type=int, default=None,
                         help="BCM GPIO number for transceiver DE/RE, if wired")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    with WeatherStationSensor(
        port=args.port,
        baudrate=args.baud,
        slave_addr=args.address,
        de_re_pin=args.de_re_pin,
    ) as sensor:
        while True:
            try:
                reading = sensor.read_all()
                print(reading)
            except ModbusError as e:
                print(f"Read failed: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
