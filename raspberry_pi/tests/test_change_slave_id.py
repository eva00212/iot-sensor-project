"""
test_change_slave_id.py

Unit tests for change_slave_id.py's pure logic (FC06 frame building, echo
verification, and the read-back verification's outcome interpretation)
using a mocked serial layer -- no real RS485 hardware or sensor required,
and nothing here ever performs a real write.

Run with:
  python -m unittest discover raspberry_pi/tests
"""

import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import modbus_poller
import change_slave_id


class TestBuildWriteRequest(unittest.TestCase):
    def test_frame_shape_and_function_code(self):
        frame = change_slave_id._build_write_request(1, 0x07D0, 5)
        self.assertEqual(len(frame), 8)  # addr+func+reg(2)+value(2)+crc(2)
        self.assertEqual(frame[0], 1)
        self.assertEqual(frame[1], change_slave_id.WRITE_SINGLE_REGISTER)
        self.assertEqual(frame[1], 0x06)

    def test_register_and_value_encoded_big_endian(self):
        frame = change_slave_id._build_write_request(1, 0x07D0, 5)
        register = struct.unpack(">H", frame[2:4])[0]
        value = struct.unpack(">H", frame[4:6])[0]
        self.assertEqual(register, 0x07D0)
        self.assertEqual(value, 5)

    def test_crc_matches_module_crc_implementation(self):
        # Locks in that this reuses modbus_poller's actual CRC function
        # rather than a separate/copied implementation that could drift.
        frame = change_slave_id._build_write_request(1, 0x07D0, 5)
        body, crc_bytes = frame[:-2], frame[-2:]
        expected_crc = modbus_poller._crc16_modbus(body)
        received_crc = crc_bytes[0] | (crc_bytes[1] << 8)
        self.assertEqual(received_crc, expected_crc)

    def test_different_slave_addresses_only_change_address_byte_and_crc(self):
        frame_1 = change_slave_id._build_write_request(1, 0x07D0, 5)
        frame_2 = change_slave_id._build_write_request(2, 0x07D0, 5)
        self.assertNotEqual(frame_1[0], frame_2[0])
        self.assertEqual(frame_1[1:6], frame_2[1:6])  # func/register/value identical
        self.assertNotEqual(frame_1[-2:], frame_2[-2:])  # CRC differs since the body differs


class TestVerifyEcho(unittest.TestCase):
    def test_exact_match_is_verified(self):
        frame = change_slave_id._build_write_request(1, 0x07D0, 5)
        self.assertTrue(change_slave_id._verify_echo(frame, frame))

    def test_mismatch_is_not_verified(self):
        request = change_slave_id._build_write_request(1, 0x07D0, 5)
        garbled = bytearray(request)
        garbled[-1] ^= 0xFF
        self.assertFalse(change_slave_id._verify_echo(request, bytes(garbled)))


class TestWriteSingleRegister(unittest.TestCase):
    """Exercises _write_single_register's response validation against a
    fake serial.Serial (via modbus_poller._get_serial), matching
    modbus_poller._read_registers_once's own validation rigor."""

    def _fake_serial(self, response: bytes):
        fake = MagicMock()
        fake.is_open = True
        fake.read.return_value = response
        return fake

    def test_success_returns_echoed_response(self):
        request = change_slave_id._build_write_request(1, 0x07D0, 5)
        fake = self._fake_serial(request)  # a valid FC06 response echoes the request

        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            response = change_slave_id._write_single_register(1, 0x07D0, 5)

        self.assertEqual(response, request)
        fake.reset_input_buffer.assert_called_once()
        fake.flush.assert_called_once()

    def test_short_response_raises(self):
        fake = self._fake_serial(b"\x01\x06")
        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                change_slave_id._write_single_register(1, 0x07D0, 5)

    def test_no_response_raises(self):
        fake = self._fake_serial(b"")
        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                change_slave_id._write_single_register(1, 0x07D0, 5)

    def test_exception_response_raises(self):
        # Function code with the high bit set signals a Modbus exception
        # (e.g. ILLEGAL DATA ADDRESS if the register isn't writable);
        # third byte is the exception code.
        body = bytes([0x01, 0x06 | 0x80, 0x02])
        crc = modbus_poller._crc16_modbus(body)
        response = body + struct.pack("<H", crc)
        fake = self._fake_serial(response)

        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                change_slave_id._write_single_register(1, 0x07D0, 5)

    def test_crc_mismatch_on_full_length_response_raises(self):
        request = change_slave_id._build_write_request(1, 0x07D0, 5)
        corrupted = bytearray(request)
        corrupted[-1] ^= 0xFF
        fake = self._fake_serial(bytes(corrupted))

        with patch.object(modbus_poller, "_get_serial", return_value=fake):
            with self.assertRaises(modbus_poller.ModbusError):
                change_slave_id._write_single_register(1, 0x07D0, 5)


class TestTryRead(unittest.TestCase):
    def test_returns_true_on_first_success(self):
        with patch.object(modbus_poller, "_read_registers_once", return_value=[650]) as mock_read, \
             patch.object(change_slave_id.time, "sleep"):
            self.assertTrue(change_slave_id._try_read(5))
        self.assertEqual(mock_read.call_count, 1)

    def test_retries_before_giving_up(self):
        with patch.object(
            modbus_poller, "_read_registers_once",
            side_effect=modbus_poller.ModbusError("no response"),
        ) as mock_read, \
             patch.object(change_slave_id.time, "sleep") as mock_sleep:
            self.assertFalse(change_slave_id._try_read(5, attempts=3))

        self.assertEqual(mock_read.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 3)

    def test_succeeds_after_transient_failure(self):
        with patch.object(
            modbus_poller, "_read_registers_once",
            side_effect=[modbus_poller.ModbusError("noise"), [650]],
        ), patch.object(change_slave_id.time, "sleep"):
            self.assertTrue(change_slave_id._try_read(5, attempts=3))


if __name__ == "__main__":
    unittest.main()
