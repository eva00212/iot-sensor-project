"""
test_modbus_poller.py

Unit tests for modbus_poller.py's pure logic (register offset math, signed
conversion, scaling, rain_detected derivation, retry handling) using a
mocked serial layer — no real RS485 hardware or port required.

Run with:
  python -m unittest discover raspberry_pi/tests
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import modbus_poller


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


class TestPollIndoor(unittest.TestCase):
    def test_success(self):
        # Block is registers 504..507: [humidity, temperature, unused, co2]
        fake_regs = [654, 251, 0, 453]  # humidity=65.4, temp=25.1, co2=453
        with patch.object(modbus_poller, "_read_block", return_value=fake_regs):
            payload = modbus_poller.poll_indoor("testBed01", "device01", 1)

        self.assertEqual(payload["site_id"], "testBed01")
        self.assertEqual(payload["device_id"], "device01")
        self.assertEqual(payload["temperature"], 25.1)
        self.assertEqual(payload["humidity"], 65.4)
        self.assertEqual(payload["co2"], 453)
        self.assertEqual(payload["device_fault"], "false")
        self.assertIn("timestamp", payload)

    def test_negative_temperature(self):
        fake_regs = [654, 0xFF9C, 0, 453]  # temp register = -100 raw -> -10.0 degC
        with patch.object(modbus_poller, "_read_block", return_value=fake_regs):
            payload = modbus_poller.poll_indoor("testBed01", "device02", 2)

        self.assertEqual(payload["temperature"], -10.0)

    def test_failure_sets_fault_and_omits_co2(self):
        with patch.object(modbus_poller, "_read_block", return_value=None):
            payload = modbus_poller.poll_indoor("testBed01", "device01", 1)

        self.assertEqual(payload["device_fault"], "true")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["humidity"], 0.0)
        self.assertNotIn("co2", payload)


class TestPollOutdoor(unittest.TestCase):
    def _make_regs(self, wind=14, humidity=614, temperature=238, rainfall=0, solar=6536):
        """Builds a 16-register block (500..515) with only the fields the
        poller reads populated; everything else is filler."""
        regs = [0] * modbus_poller.OUTDOOR_REG_COUNT
        regs[modbus_poller.OUTDOOR_OFF_WIND]  = wind
        regs[modbus_poller.OUTDOOR_OFF_HUMID] = humidity
        regs[modbus_poller.OUTDOOR_OFF_TEMP]  = temperature
        regs[modbus_poller.OUTDOOR_OFF_RAIN]  = rainfall
        regs[modbus_poller.OUTDOOR_OFF_SOLAR] = solar
        return regs

    def test_success_no_rain(self):
        fake_regs = self._make_regs(rainfall=0)
        with patch.object(modbus_poller, "_read_block", return_value=fake_regs):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["wind_speed"], 1.4)
        self.assertEqual(payload["humidity"], 61.4)
        self.assertEqual(payload["temperature"], 23.8)
        self.assertEqual(payload["solar_radiation"], 6536.0)
        self.assertEqual(payload["rain_detected"], "false")
        self.assertEqual(payload["device_fault"], "false")
        self.assertNotIn("rainfall", payload)

    def test_success_with_rain(self):
        fake_regs = self._make_regs(rainfall=5)  # 5 x 0.1mm = 0.5mm -> rain detected
        with patch.object(modbus_poller, "_read_block", return_value=fake_regs):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["rain_detected"], "true")
        self.assertNotIn("rainfall", payload)

    def test_failure_sets_fault_and_omits_optional_fields(self):
        with patch.object(modbus_poller, "_read_block", return_value=None):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["device_fault"], "true")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["humidity"], 0.0)
        self.assertEqual(payload["rain_detected"], "false")
        self.assertNotIn("wind_speed", payload)
        self.assertNotIn("solar_radiation", payload)


class TestReadBlockRetry(unittest.TestCase):
    def test_succeeds_after_transient_failures(self):
        fake_instrument = MagicMock()
        fake_instrument.read_registers.side_effect = [
            modbus_poller.minimalmodbus.NoResponseError("timeout"),
            [1, 2, 3, 4],
        ]
        with patch.object(modbus_poller, "_get_instrument", return_value=fake_instrument):
            result = modbus_poller._read_block(1, 504, 4)

        self.assertEqual(result, [1, 2, 3, 4])
        self.assertEqual(fake_instrument.read_registers.call_count, 2)

    def test_returns_none_after_exhausting_retries(self):
        fake_instrument = MagicMock()
        fake_instrument.read_registers.side_effect = modbus_poller.minimalmodbus.InvalidResponseError("bad CRC")

        with patch.object(modbus_poller, "_get_instrument", return_value=fake_instrument):
            result = modbus_poller._read_block(1, 504, 4)

        self.assertIsNone(result)
        self.assertEqual(fake_instrument.read_registers.call_count, modbus_poller.MAX_RETRIES)


if __name__ == "__main__":
    unittest.main()
