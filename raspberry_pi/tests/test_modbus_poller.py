"""
test_modbus_poller.py

Unit tests for modbus_poller.py's pure logic (register offset math, signed
conversion, scaling, rain_detected derivation, retry handling, and the
per-field split-read pattern) using a mocked serial layer — no real RS485
hardware or port required.

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

    def test_manual_worked_example(self):
        # Manual section 4.4.3: 0xFF9B -> -101 -> -10.1 degC after x0.1 scale
        self.assertEqual(modbus_poller._to_signed16(0xFF9B), -101)


class TestPollIndoor(unittest.TestCase):
    """poll_indoor does two separate reads: registers 504-505
    (humidity+temperature, the manual's own worked example) and register
    507 (CO2) on its own."""

    def _mock_read_block(self, ht=(654, 251), co2=453, ht_fail=False, co2_fail=False):
        def fake(slave_addr, start_reg, count):
            if start_reg == modbus_poller.HUMID_TEMP_REG_START:
                self.assertEqual(count, 2)
                return (None, "ht read failed") if ht_fail else (list(ht), None)
            if start_reg == modbus_poller.REG_CO2:
                self.assertEqual(count, 1)
                return (None, "co2 read failed") if co2_fail else ([co2], None)
            raise AssertionError(f"unexpected register read: start={start_reg} count={count}")
        return fake

    def test_success(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block()), \
             patch.object(modbus_poller.time, "sleep"):
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
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(ht=(654, 0xFF9C))), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_indoor("testBed01", "device02", 2)

        self.assertEqual(payload["temperature"], -10.0)

    def test_humid_temp_failure_uses_fallback_but_still_reads_co2(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(ht_fail=True)), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_indoor("testBed01", "device01", 1)

        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["humidity"], 0.0)
        self.assertEqual(payload["co2"], 453)  # co2 read is independent, still succeeded
        self.assertEqual(payload["device_fault"], "true")
        self.assertIn("ht read failed", payload["error_message"])

    def test_co2_failure_omits_co2_but_keeps_humid_temp(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(co2_fail=True)), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_indoor("testBed01", "device01", 1)

        self.assertEqual(payload["temperature"], 25.1)
        self.assertEqual(payload["humidity"], 65.4)
        self.assertNotIn("co2", payload)
        self.assertEqual(payload["device_fault"], "true")  # any sub-read failing -> fault
        self.assertIn("co2 read failed", payload["error_message"])

    def test_both_reads_fail(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(ht_fail=True, co2_fail=True)), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_indoor("testBed01", "device01", 1)

        self.assertEqual(payload["device_fault"], "true")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["humidity"], 0.0)
        self.assertNotIn("co2", payload)
        self.assertIn("ht read failed", payload["error_message"])
        self.assertIn("co2 read failed", payload["error_message"])


class TestPollOutdoor(unittest.TestCase):
    """poll_outdoor does four separate reads: register 500 (wind speed,
    the manual's worked example), registers 504-505 (humidity+temperature,
    the manual's other worked example), register 513 (rainfall), and
    register 515 (solar radiation) -- each independently, matching the
    manual's proven pattern rather than one wide block read."""

    def _mock_read_block(self, wind=14, ht=(614, 238), rain=0, solar=6536,
                          wind_fail=False, ht_fail=False, rain_fail=False, solar_fail=False):
        def fake(slave_addr, start_reg, count):
            if start_reg == modbus_poller.REG_WIND_SPEED:
                self.assertEqual(count, 1)
                return (None, "wind read failed") if wind_fail else ([wind], None)
            if start_reg == modbus_poller.HUMID_TEMP_REG_START:
                self.assertEqual(count, 2)
                return (None, "ht read failed") if ht_fail else (list(ht), None)
            if start_reg == modbus_poller.REG_RAINFALL:
                self.assertEqual(count, 1)
                return (None, "rain read failed") if rain_fail else ([rain], None)
            if start_reg == modbus_poller.REG_SOLAR:
                self.assertEqual(count, 1)
                return (None, "solar read failed") if solar_fail else ([solar], None)
            raise AssertionError(f"unexpected register read: start={start_reg} count={count}")
        return fake

    def test_success_no_rain(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(rain=0)), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["wind_speed"], 1.4)
        self.assertEqual(payload["humidity"], 61.4)
        self.assertEqual(payload["temperature"], 23.8)
        self.assertEqual(payload["solar_radiation"], 6536.0)
        self.assertEqual(payload["rain_detected"], "false")
        self.assertEqual(payload["device_fault"], "false")
        self.assertNotIn("rainfall", payload)
        self.assertNotIn("error_message", payload)

    def test_success_with_rain(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(rain=5)), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["rain_detected"], "true")
        self.assertNotIn("rainfall", payload)

    def test_all_reads_fail(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(
                wind_fail=True, ht_fail=True, rain_fail=True, solar_fail=True)), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertEqual(payload["device_fault"], "true")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["humidity"], 0.0)
        self.assertEqual(payload["rain_detected"], "false")
        self.assertNotIn("wind_speed", payload)
        self.assertNotIn("solar_radiation", payload)
        for expected in ("wind read failed", "ht read failed", "rain read failed", "solar read failed"):
            self.assertIn(expected, payload["error_message"])

    def test_partial_failure_wind_only_still_reports_rest(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block(wind_fail=True)), \
             patch.object(modbus_poller.time, "sleep"):
            payload = modbus_poller.poll_outdoor("testBed01", "device03", 3)

        self.assertNotIn("wind_speed", payload)
        self.assertEqual(payload["temperature"], 23.8)
        self.assertEqual(payload["humidity"], 61.4)
        self.assertEqual(payload["solar_radiation"], 6536.0)
        self.assertEqual(payload["device_fault"], "true")  # any sub-read failing -> fault
        self.assertEqual(payload["error_message"], "wind read failed")

    def test_inter_read_spacing_applied_between_each_sub_read(self):
        with patch.object(modbus_poller, "_read_block", side_effect=self._mock_read_block()), \
             patch.object(modbus_poller.time, "sleep") as mock_sleep:
            modbus_poller.poll_outdoor("testBed01", "device03", 3)

        # 4 reads -> 3 gaps between them
        self.assertEqual(mock_sleep.call_count, 3)
        for call in mock_sleep.call_args_list:
            self.assertEqual(call.args[0], modbus_poller.INTER_READ_DELAY_SEC)


class TestReadBlockRetry(unittest.TestCase):
    """_read_block now returns (values, error_message) instead of a bare
    list, and sleeps RETRY_DELAY_SEC between attempts -- time.sleep is
    mocked throughout so these tests don't actually wait."""

    def test_succeeds_after_transient_failures(self):
        fake_instrument = MagicMock()
        fake_instrument.read_registers.side_effect = [
            modbus_poller.minimalmodbus.NoResponseError("timeout"),
            [1, 2, 3, 4],
        ]
        with patch.object(modbus_poller, "_get_instrument", return_value=fake_instrument), \
             patch.object(modbus_poller.time, "sleep") as mock_sleep:
            values, error = modbus_poller._read_block(1, 504, 4)

        self.assertEqual(values, [1, 2, 3, 4])
        self.assertIsNone(error)
        self.assertEqual(fake_instrument.read_registers.call_count, 2)
        mock_sleep.assert_called_once_with(modbus_poller.RETRY_DELAY_SEC)

    def test_returns_none_after_exhausting_retries(self):
        fake_instrument = MagicMock()
        fake_instrument.read_registers.side_effect = modbus_poller.minimalmodbus.InvalidResponseError("bad CRC")

        with patch.object(modbus_poller, "_get_instrument", return_value=fake_instrument), \
             patch.object(modbus_poller.time, "sleep") as mock_sleep:
            values, error = modbus_poller._read_block(1, 504, 4)

        self.assertIsNone(values)
        self.assertIn("bad CRC", error)
        self.assertEqual(fake_instrument.read_registers.call_count, modbus_poller.MAX_RETRIES)
        # One sleep between each pair of attempts, none after the last.
        self.assertEqual(mock_sleep.call_count, modbus_poller.MAX_RETRIES - 1)

    def test_retries_when_instrument_acquisition_itself_fails(self):
        # e.g. /dev/serial0 doesn't exist yet right after boot -- opening
        # the port (inside _get_instrument) fails, not the read. This must
        # be retried exactly like a failed read, not raise out of
        # _read_block.
        fake_instrument = MagicMock()
        fake_instrument.read_registers.return_value = [1, 2, 3, 4]

        with patch.object(
            modbus_poller, "_get_instrument",
            side_effect=[modbus_poller.serial.SerialException("port busy"), fake_instrument],
        ) as mock_get, \
             patch.object(modbus_poller.time, "sleep"):
            values, error = modbus_poller._read_block(1, 504, 4)

        self.assertEqual(values, [1, 2, 3, 4])
        self.assertIsNone(error)
        self.assertEqual(mock_get.call_count, 2)

    def test_returns_none_when_instrument_acquisition_always_fails(self):
        with patch.object(
            modbus_poller, "_get_instrument",
            side_effect=modbus_poller.serial.SerialException("no such device"),
        ) as mock_get, \
             patch.object(modbus_poller.time, "sleep"):
            values, error = modbus_poller._read_block(1, 504, 4)

        self.assertIsNone(values)
        self.assertIn("no such device", error)
        self.assertEqual(mock_get.call_count, modbus_poller.MAX_RETRIES)


if __name__ == "__main__":
    unittest.main()
