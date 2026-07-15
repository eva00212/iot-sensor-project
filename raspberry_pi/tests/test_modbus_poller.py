"""
test_modbus_poller.py

Unit tests for modbus_poller.py's pure logic (CRC16, frame building,
response parsing, register offset math, signed conversion, scaling,
rain_detected derivation, retry handling, and the single-block-read
strategy) using a mocked serial layer -- no real RS485 hardware or port
required.

modbus_poller.py's low-level Modbus RTU handling is a hand-rolled port of
rs485_weather_station.py's WeatherStationSensor (raw pyserial + manual
CRC16/framing, not the minimalmodbus library), verified working against
real hardware. TestCrc16/TestBuildRequest lock in that the hand-rolled
framing matches the sensor manual's own worked examples exactly, since
there's no library to lean on for correctness anymore.

Run with:
  python -m unittest discover raspberry_pi/tests
"""

import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import modbus_poller


def _build_response(slave_addr: int, values: list[int]) -> bytes:
    """Builds a valid Modbus RTU response frame for `values`, using the
    module's own CRC so these tests stay correct even if framing details
    change, as long as _read_registers_once's parsing is exercised
    realistically."""
    body = bytes([slave_addr, modbus_poller.READ_HOLDING_REGISTERS, 2 * len(values)])
    for v in values:
        body += struct.pack(">H", v & 0xFFFF)
    crc = modbus_poller._crc16_modbus(body)
    return body + struct.pack("<H", crc)


def _full_block_registers(wind=14, humidity=654, temperature=251, co2=453, rainfall=0, solar=6536):
    """Builds a 16-element register list (indices 0..15 == registers
    500..515) with the given values at their real offsets and zero
    elsewhere, matching what a real 500-515 block read returns."""
    regs = [0] * modbus_poller.REGISTER_COUNT
    regs[modbus_poller.REG_WIND_SPEED - modbus_poller.FIRST_REGISTER]  = wind & 0xFFFF
    regs[modbus_poller.REG_HUMIDITY - modbus_poller.FIRST_REGISTER]    = humidity
    regs[modbus_poller.REG_TEMPERATURE - modbus_poller.FIRST_REGISTER] = temperature & 0xFFFF
    regs[modbus_poller.REG_CO2 - modbus_poller.FIRST_REGISTER]         = co2
    regs[modbus_poller.REG_RAINFALL - modbus_poller.FIRST_REGISTER]    = rainfall
    regs[modbus_poller.REG_SOLAR - modbus_poller.FIRST_REGISTER]       = solar
    return regs


class TestToSigned16(unittest.TestCase):
    def test_positive_value_unchanged(self):
        self.assertEqual(modbus_poller._to_signed16(251), 251)

    def test_zero(self):
        self.assertEqual(modbus_poller._to_signed16(0), 0)

    def test_max_positive_boundary(self):
        self.assertEqual(modbus_poller._to_signed16(0x7FFF), 32767)

    def test_negative_value_converted(self):
        # 0xFF9C = 65436 -> -100 (represents -10.0 degC after x0.1 scale)
        self.assertEqual(modbus_poller._to_signed16(0xFF9C), -100)

    def test_min_negative_boundary(self):
        self.assertEqual(modbus_poller._to_signed16(0x8000), -32768)

    def test_manual_worked_example(self):
        # Manual section 4.4.3: 0xFF9B -> -101 -> -10.1 degC after x0.1 scale
        self.assertEqual(modbus_poller._to_signed16(0xFF9B), -101)


class TestCrc16(unittest.TestCase):
    def test_matches_manual_worked_example(self):
        # Manual 4.4.1: query for wind speed at slave 0x01, start 0x01F4,
        # qty 1 -> CRC 0xC4 0x04 (low byte first on the wire).
        frame = bytes([0x01, 0x03, 0x01, 0xF4, 0x00, 0x01])
        crc = modbus_poller._crc16_modbus(frame)
        self.assertEqual(crc & 0xFF, 0xC4)
        self.assertEqual((crc >> 8) & 0xFF, 0x04)


class TestBuildRequest(unittest.TestCase):
    """Verifies the hand-rolled frame building produces byte-identical
    requests to the sensor manual's own worked examples -- the same
    frames tools/test_modbus_sensor.py's --self-test checks minimalmodbus
    against."""

    def test_wind_speed_worked_example(self):
        # Manual 4.4.1: slave 1, func 3, reg 500, count 1
        expected = bytes.fromhex("01 03 01 F4 00 01 C4 04".replace(" ", ""))
        self.assertEqual(modbus_poller._build_request(1, 500, 1), expected)

    def test_humidity_temp_worked_example(self):
        # Manual 4.4.3: slave 1, func 3, reg 504, count 2
        expected = bytes.fromhex("01 03 01 F8 00 02 44 06".replace(" ", ""))
        self.assertEqual(modbus_poller._build_request(1, 504, 2), expected)

    def test_full_block_read_different_slave_addresses(self):
        # Only the address byte and CRC should change across slaves;
        # function code and register range stay the same for every device.
        for slave in (1, 2, 3):
            frame = modbus_poller._build_request(slave, 500, 16)
            self.assertEqual(frame[0], slave)
            self.assertEqual(frame[1], modbus_poller.READ_HOLDING_REGISTERS)


class TestReadRegistersOnce(unittest.TestCase):
    """Exercises _read_registers_once's response parsing against a fake
    serial.Serial (via _get_serial), matching
    rs485_weather_station.py's read_registers() logic exactly."""

    def _fake_serial(self, response: bytes):
        fake = MagicMock()
        fake.is_open = True
        fake.read.return_value = response
        return fake

    def test_success_full_block(self):
        regs = _full_block_registers()
        response = _build_response(1, regs)
        fake = self._fake_serial(response)

        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            result = modbus_poller._read_registers_once(1, modbus_poller.FIRST_REGISTER, modbus_poller.REGISTER_COUNT)

        self.assertEqual(result, regs)
        fake.reset_input_buffer.assert_called_once()
        fake.flush.assert_called_once()
        written = fake.write.call_args[0][0]
        self.assertEqual(written, modbus_poller._build_request(1, modbus_poller.FIRST_REGISTER, modbus_poller.REGISTER_COUNT))

    def test_short_response_raises(self):
        fake = self._fake_serial(b"\x01\x03")  # far too short
        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                modbus_poller._read_registers_once(1, 500, 16)

    def test_no_response_raises(self):
        fake = self._fake_serial(b"")
        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                modbus_poller._read_registers_once(1, 500, 16)

    def test_slave_exception_response_raises(self):
        # Function code with the high bit set signals a Modbus exception;
        # third byte is the exception code.
        body = bytes([0x01, 0x03 | 0x80, 0x02])
        crc = modbus_poller._crc16_modbus(body)
        response = body + struct.pack("<H", crc)
        fake = self._fake_serial(response)

        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                modbus_poller._read_registers_once(1, 500, 16)

    def test_wrong_slave_address_in_response_raises(self):
        response = _build_response(2, _full_block_registers())  # asked slave 1, got slave 2's reply
        fake = self._fake_serial(response)

        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                modbus_poller._read_registers_once(1, 500, 16)

    def test_crc_mismatch_raises(self):
        response = bytearray(_build_response(1, _full_block_registers()))
        response[-1] ^= 0xFF  # corrupt the CRC
        fake = self._fake_serial(bytes(response))

        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                modbus_poller._read_registers_once(1, 500, 16)


class TestReadBlockRetry(unittest.TestCase):
    """_read_block returns (values, error_message) and sleeps
    RETRY_DELAY_SEC between attempts -- time.sleep is mocked throughout so
    these tests don't actually wait."""

    def test_succeeds_after_transient_failures(self):
        regs = _full_block_registers()
        with patch.object(
            modbus_poller, "_read_registers_once",
            side_effect=[modbus_poller.ModbusError("no response"), regs],
        ) as mock_read, \
             patch.object(modbus_poller.time, "sleep") as mock_sleep:
            values, error = modbus_poller._read_block(1)

        self.assertEqual(values, regs)
        self.assertIsNone(error)
        self.assertEqual(mock_read.call_count, 2)
        mock_sleep.assert_called_once_with(modbus_poller.RETRY_DELAY_SEC)

    def test_returns_none_after_exhausting_retries(self):
        with patch.object(
            modbus_poller, "_read_registers_once",
            side_effect=modbus_poller.ModbusError("CRC mismatch"),
        ) as mock_read, \
             patch.object(modbus_poller.time, "sleep") as mock_sleep:
            values, error = modbus_poller._read_block(1)

        self.assertIsNone(values)
        self.assertIn("CRC mismatch", error)
        self.assertEqual(mock_read.call_count, modbus_poller.MAX_RETRIES)
        # One sleep between each pair of attempts, none after the last.
        self.assertEqual(mock_sleep.call_count, modbus_poller.MAX_RETRIES - 1)

    def test_retries_when_serial_port_acquisition_itself_fails(self):
        # e.g. /dev/serial0 doesn't exist yet right after boot.
        regs = _full_block_registers()
        with patch.object(
            modbus_poller, "_read_registers_once",
            side_effect=[modbus_poller.serial.SerialException("port busy"), regs],
        ) as mock_read, \
             patch.object(modbus_poller.time, "sleep"):
            values, error = modbus_poller._read_block(1)

        self.assertEqual(values, regs)
        self.assertIsNone(error)
        self.assertEqual(mock_read.call_count, 2)


class TestPollIndoor(unittest.TestCase):
    """poll_indoor now does exactly one block read (500-515) instead of
    separate per-field reads -- success or failure applies to the whole
    reading, not per field."""

    def test_success(self):
        regs = _full_block_registers(humidity=654, temperature=251, co2=453)
        with patch.object(modbus_poller, "_read_block", return_value=(regs, None)):
            payload = modbus_poller.poll_indoor("testBed01", "device01", 1)

        self.assertEqual(payload["site_id"], "testBed01")
        self.assertEqual(payload["device_id"], "device01")
        self.assertEqual(payload["temperature"], 25.1)
        self.assertEqual(payload["humidity"], 65.4)
        self.assertEqual(payload["co2"], 453)
        self.assertEqual(payload["device_fault"], "false")
        self.assertNotIn("error_message", payload)
        self.assertIn("timestamp", payload)

    def test_negative_temperature(self):
        regs = _full_block_registers(temperature=0xFF9C)
        with patch.object(modbus_poller, "_read_block", return_value=(regs, None)):
            payload = modbus_poller.poll_indoor("testBed01", "device02", 2)

        self.assertEqual(payload["temperature"], -10.0)

    def test_read_failure_falls_back_and_reports_error(self):
        with patch.object(modbus_poller, "_read_block", return_value=(None, "no response from slave")):
            payload = modbus_poller.poll_indoor("testBed01", "device01", 1)

        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["humidity"], 0.0)
        self.assertNotIn("co2", payload)
        self.assertEqual(payload["device_fault"], "true")
        self.assertEqual(payload["error_message"], "no response from slave")

    def test_reads_full_block_starting_at_first_register(self):
        with patch.object(modbus_poller, "_read_block", return_value=(_full_block_registers(), None)) as mock_read:
            modbus_poller.poll_indoor("testBed01", "device01", 1)

        mock_read.assert_called_once_with(1)


class TestPollOutdoor(unittest.TestCase):
    """poll_outdoor now does exactly one block read (500-515) instead of
    four separate reads -- success or failure applies to the whole
    reading, not per field."""

    def test_success_no_rain(self):
        regs = _full_block_registers(wind=14, humidity=614, temperature=238, rainfall=0, solar=6536)
        with patch.object(modbus_poller, "_read_block", return_value=(regs, None)):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["wind_speed"], 1.4)
        self.assertEqual(payload["humidity"], 61.4)
        self.assertEqual(payload["temperature"], 23.8)
        self.assertEqual(payload["solar_radiation"], 6536.0)
        self.assertEqual(payload["rain_detected"], "false")
        self.assertEqual(payload["device_fault"], "false")
        self.assertNotIn("error_message", payload)

    def test_success_with_rain(self):
        regs = _full_block_registers(rainfall=5)
        with patch.object(modbus_poller, "_read_block", return_value=(regs, None)):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["rain_detected"], "true")

    def test_read_failure_falls_back_and_reports_error(self):
        with patch.object(modbus_poller, "_read_block", return_value=(None, "CRC mismatch")):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["device_fault"], "true")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["humidity"], 0.0)
        self.assertEqual(payload["rain_detected"], "false")
        self.assertNotIn("wind_speed", payload)
        self.assertNotIn("solar_radiation", payload)
        self.assertEqual(payload["error_message"], "CRC mismatch")

    def test_reads_full_block_starting_at_first_register(self):
        with patch.object(modbus_poller, "_read_block", return_value=(_full_block_registers(), None)) as mock_read:
            modbus_poller.poll_outdoor("testBed01", "device03", 3)

        mock_read.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
