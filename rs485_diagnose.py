"""
Low-level diagnostic helper for debugging "no response" from the RS485
weather station on a Raspberry Pi 5. Use this BEFORE trusting the
higher-level rs485_weather_station.py driver -- it shows raw bytes so you
can see exactly where the chain is breaking.

Usage:
  # 1. UART loopback test (isolates the Pi/OS from the RS485 hardware).
  #    Physically disconnect the sensor. On the header, jumper GPIO14
  #    (pin 8, TX) directly to GPIO15 (pin 10, RX) with a single wire.
  python3 rs485_diagnose.py --loopback

  # 2. Raw query test against the sensor (after removing the loopback
  #    jumper and reconnecting the sensor). Prints exactly what came
  #    back, even if it's 0 bytes or garbage.
  python3 rs485_diagnose.py --query --baud 4800 --address 1
  python3 rs485_diagnose.py --query --baud 9600 --address 1
  python3 rs485_diagnose.py --query --baud 2400 --address 1
"""

import argparse
import time

import serial

# Exact bytes from the datasheet's own worked example (section 4.4.1):
# read 1 register starting at address 500 (0x01F4) from slave 0x01.
KNOWN_GOOD_FRAME = bytes([0x01, 0x03, 0x01, 0xF4, 0x00, 0x01, 0xC4, 0x04])


def build_query(address: int) -> bytes:
    if address == 1:
        return KNOWN_GOOD_FRAME
    # recompute CRC for a non-default address
    import struct

    def crc16(data: bytes) -> int:
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return crc

    body = struct.pack(">BBHH", address, 0x03, 500, 1)
    crc = crc16(body)
    return body + struct.pack("<H", crc)


def loopback_test(port: str, baud: int) -> None:
    ser = serial.Serial(port, baud, timeout=1.0)
    pattern = bytes(range(0, 16))
    ser.reset_input_buffer()
    ser.write(pattern)
    ser.flush()
    time.sleep(0.1)
    received = ser.read(len(pattern))
    ser.close()

    print(f"Sent:     {pattern.hex()}")
    print(f"Received: {received.hex() if received else '(nothing)'}")
    if received == pattern:
        print("PASS: UART TX/RX loopback works. The Pi's serial port and "
              "OS config are fine -- the problem is downstream (RS485 "
              "transceiver, wiring, power, or the sensor itself).")
    elif not received:
        print("FAIL: Got nothing back at all. Either the jumper wire "
              "isn't actually bridging pin 8 to pin 10, the port isn't "
              "the one you think it is, or something else (getty, "
              "another process) is holding/consuming the port. Check:\n"
              "  sudo lsof " + port + "\n"
              "  cat /proc/cmdline   (must NOT contain console=serial0)\n"
              "  systemctl status serial-getty@*.service")
    else:
        print("FAIL: Got something back, but it doesn't match what was "
              "sent -- possible baud mismatch or noise.")


def query_test(port: str, baud: int, address: int) -> None:
    ser = serial.Serial(
        port, baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=1.0,
    )
    frame = build_query(address)
    ser.reset_input_buffer()
    print(f"Port={port} baud={baud} address={address:#04x}")
    print(f"TX: {frame.hex(' ')}")
    ser.write(frame)
    ser.flush()
    time.sleep(0.2)
    n_waiting = ser.in_waiting
    received = ser.read(64)
    ser.close()

    if not received:
        print("RX: (nothing) -- 0 bytes in, 0 bytes out.")
    else:
        print(f"RX: {received.hex(' ')}  ({len(received)} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/serial0")
    parser.add_argument("--baud", type=int, default=4800)
    parser.add_argument("--address", type=lambda x: int(x, 0), default=1)
    parser.add_argument("--loopback", action="store_true")
    parser.add_argument("--query", action="store_true")
    args = parser.parse_args()

    if args.loopback:
        loopback_test(args.port, args.baud)
    elif args.query:
        query_test(args.port, args.baud, args.address)
    else:
        parser.error("pass --loopback or --query")


if __name__ == "__main__":
    main()
