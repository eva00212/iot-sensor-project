#!/usr/bin/env python3
"""
test_modbus_sensor.py

Standalone diagnostic tool for ONE RS485 Modbus RTU sensor. Talks to the
sensor directly via minimalmodbus - no dependency on the rest of this
project (no collector.py, modbus_poller.py, MQTT, config files, etc.).

Use this to isolate whether a single sensor is responding on the RS485
bus at all, independent of the collector pipeline.

Examples:
  python test_modbus_sensor.py
  python test_modbus_sensor.py --slave 0x02 --start-register 504 --count 4
  python test_modbus_sensor.py --port /dev/ttyUSB0 --slave 3 --start-register 500 --count 16 --baudrate 9600
  python test_modbus_sensor.py --raw-listen
"""

import argparse
import sys
import time

try:
    import minimalmodbus
    import serial
except ImportError as e:
    print(f"[FATAL] Missing dependency: {e}")
    print("Install with: pip install minimalmodbus pyserial")
    sys.exit(1)


# Known register meanings, purely for decoding convenience (matches the
# project's confirmed sensor manual). Reading an address not in this map
# still works fine -- it's just shown as a raw, unscaled value.
KNOWN_REGISTERS = {
    500: ("wind_speed",      0.1, True),   # (name, scale, signed)
    504: ("humidity",        0.1, False),
    505: ("temperature",     0.1, True),
    507: ("co2",             1.0, False),
    513: ("rainfall",        0.1, False),
    515: ("solar_radiation", 1.0, False),
}

PARITY_MAP = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}


def to_signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


def describe_register(addr: int, raw: int) -> str:
    meta = KNOWN_REGISTERS.get(addr)
    if meta is None:
        return f"  reg {addr}: raw={raw}  (unknown register -- no scaling applied)"
    name, scale, signed = meta
    value = to_signed16(raw) if signed else raw
    return f"  reg {addr} ({name}): raw={raw} -> {value * scale:g}"


def parse_args():
    p = argparse.ArgumentParser(
        description="Standalone RS485 Modbus RTU sensor test (no project dependencies)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", default="/dev/serial0", help="Serial port device")
    p.add_argument("--slave", type=lambda x: int(x, 0), default=0x01,
                    help="Modbus slave address, decimal or 0x-hex (e.g. 1 or 0x01)")
    p.add_argument("--baudrate", type=int, default=4800)
    p.add_argument("--bytesize", type=int, default=8)
    p.add_argument("--parity", choices=list(PARITY_MAP), default="N")
    p.add_argument("--stopbits", type=int, default=1)
    p.add_argument("--timeout", type=float, default=1.0, help="Response timeout, seconds")
    p.add_argument("--function-code", type=lambda x: int(x, 0), default=3, choices=[3, 4],
                    help="3 = Read Holding Registers, 4 = Read Input Registers")
    p.add_argument("--start-register", type=int, default=505, help="First register address to read")
    p.add_argument("--count", type=int, default=1, help="Number of registers to read")
    p.add_argument("--retries", type=int, default=3, help="Attempts before giving up")
    p.add_argument("--retry-delay", type=float, default=0.5, help="Seconds between retries")
    p.add_argument("--raw-listen", action="store_true",
                    help="On failure, also open the raw serial port and listen for any bytes at "
                         "all (bypasses Modbus framing/CRC entirely) -- checks whether ANYTHING "
                         "is arriving on the wire")
    p.add_argument("--quiet", action="store_true",
                    help="Disable minimalmodbus's own verbose TX/RX byte logging")
    return p.parse_args()


def print_config(args):
    print("=" * 70)
    print("RS485 Modbus RTU standalone sensor test")
    print("=" * 70)
    print(f"  Port           : {args.port}")
    print(f"  Baud rate      : {args.baudrate}")
    print(f"  Frame          : {args.bytesize}{args.parity}{args.stopbits}")
    print(f"  Slave address  : 0x{args.slave:02X} ({args.slave})")
    fc_name = "Read Holding Registers" if args.function_code == 3 else "Read Input Registers"
    print(f"  Function code  : 0x{args.function_code:02X} ({fc_name})")
    print(f"  Register(s)    : start={args.start_register}, count={args.count}")
    print(f"  Timeout        : {args.timeout}s")
    print(f"  Retries        : {args.retries} (delay {args.retry_delay}s)")
    print("=" * 70)


def build_instrument(args):
    inst = minimalmodbus.Instrument(args.port, args.slave, mode=minimalmodbus.MODE_RTU)
    inst.serial.baudrate = args.baudrate
    inst.serial.bytesize = args.bytesize
    inst.serial.parity = PARITY_MAP[args.parity]
    inst.serial.stopbits = args.stopbits
    inst.serial.timeout = args.timeout
    inst.close_port_after_each_call = False
    inst.debug = not args.quiet  # minimalmodbus prints raw TX/RX bytes itself when True
    return inst


def raw_listen(args, duration=2.0):
    print()
    print(f"[RAW LISTEN] Opening {args.port} directly for {duration}s to check for ANY incoming bytes")
    print("[RAW LISTEN] (this bypasses Modbus framing/CRC entirely -- pure wire-level check)")
    try:
        with serial.Serial(
            port=args.port, baudrate=args.baudrate, bytesize=args.bytesize,
            parity=PARITY_MAP[args.parity], stopbits=args.stopbits, timeout=duration,
        ) as ser:
            data = ser.read(256)
            if data:
                print(f"[RAW LISTEN] Received {len(data)} byte(s): {data.hex(' ')}")
                print("[RAW LISTEN] Something IS reaching the Pi on this port -- the physical link")
                print("[RAW LISTEN] is likely fine; look at framing/baud/address/function code instead.")
            else:
                print("[RAW LISTEN] Received 0 bytes -- nothing arrived on the wire at all.")
                print("[RAW LISTEN] This points at wiring, power, or direction control, not framing.")
    except Exception as e:
        print(f"[RAW LISTEN] Failed to open port for raw listen: {type(e).__name__}: {e}")


def print_open_port_diagnosis():
    print("""
Diagnosis -- could not open the serial port at all:
  - Confirm the port path is correct:
      ls -l /dev/serial0 /dev/ttyAMA0 /dev/ttyUSB* 2>/dev/null
  - Confirm the UART is enabled for general use (not bound to the login
    console): sudo raspi-config -> Interface Options -> Serial Port
      "login shell over serial?"  -> No
      "serial hardware enabled?"  -> Yes
    then reboot.
  - Confirm your user has permission:
      groups $USER   (should include 'dialout')
    If not:
      sudo usermod -aG dialout $USER   (then log out/in)
  - If using a USB-RS485 adapter, confirm it enumerated:
      dmesg | tail -20
      ls /dev/ttyUSB*
  - Confirm no other process already has the port open (another script,
    a getty/login console, ModemManager) -- only one process can own it.
""")


def print_diagnosis(last_exception):
    print("\nFurther diagnostic steps if the sensor still isn't responding:\n")

    if isinstance(last_exception, minimalmodbus.NoResponseError):
        print("""Zero bytes received back -- this points at the physical layer, not
Modbus framing:
  1. Wiring
     - Confirm A/B (or D+/D-) aren't swapped, and match polarity end to end.
     - Confirm a common ground reference between the Pi's RS485 board and
       the sensor.
     - Check for a loose or broken connection at either end.
  2. DE/RE direction control
     - If your RS485 transceiver needs MANUAL direction control (not
       auto-direction) and it isn't wired/toggled, the query may never
       actually go out, or the reply may never make it back. Check the
       transceiver's datasheet for whether DE/RE are tied to
       auto-direction circuitry or need to be driven by a GPIO.
  3. Sensor address
     - Confirm this sensor is actually configured for slave address
       0x01 (or whatever --slave you passed) -- try the other addresses
       on this bus (0x01, 0x02, 0x03) in case they're swapped.
  4. Baud rate / power
     - Confirm the sensor is powered and any DIP switches / configuration
       match 4800 8N1.
     - Try other common baud rates (--baudrate 9600, 19200) in case the
       manual's stated default doesn't match this specific unit.
  5. Bus topology
     - Confirm 120ohm termination resistors are present at both ends of
       the bus on longer cable runs, and that this sensor is actually on
       the same physical bus segment as the Pi.""")

    elif isinstance(last_exception, minimalmodbus.InvalidResponseError):
        print("""Bytes WERE received but failed CRC/parsing -- this points at signal
integrity or a framing mismatch, not a dead link:
  1. Baud rate / frame settings
     - A near-miss baud rate (e.g. sensor actually at 9600 vs script at
       4800) often produces garbled-but-nonzero bytes. Try --baudrate
       9600 or 19200.
     - Confirm parity/stopbits really match the sensor (try --parity E
       or --stopbits 2 if the manual is ambiguous).
  2. Signal integrity
     - Long cable runs without termination resistors, or a bus with many
       stubs, can corrupt frames intermittently.
     - Check for a noise source near the cable (motors, switching PSUs).
  3. Bus contention
     - If another device on the bus shares this slave address, both may
       respond simultaneously and corrupt each other's frames.""")

    elif isinstance(last_exception, minimalmodbus.SlaveReportedException):
        print("""The sensor responded and is definitely alive on the bus, but rejected
this specific request:
  1. Register address -- try a different --start-register. The manual's
     numbering may be 0-based vs 1-based, or this variant may not
     populate every register in the shared table.
  2. Function code -- try --function-code 4 (Read Input Registers)
     instead of 3 (Read Holding Registers); some sensors put their data
     in the other table.""")

    else:
        print("""  - Re-run with --raw-listen to check whether the sensor transmits
    anything on its own. If you see bytes without sending a request, you
    may have the wrong protocol/mode, or a different device entirely at
    this address.
  - Double check nothing else on the Pi is holding the serial port open
    (another script, a getty/login console, ModemManager).""")

    print("""
General checklist:
  - Verify with a multimeter that the RS485 A/B lines show a valid
    idle-state voltage differential for your transceiver's spec.
  - If available, test the sensor against a USB-RS485 adapter and a
    known-good Modbus tool on a laptop (e.g. QModMaster, Modbus Poll,
    mbpoll) to determine whether the fault is in the sensor/wiring or in
    the Pi's RS485 interface specifically.
  - Swap in a second, known-working sensor at the same address/wiring to
    isolate whether the fault is sensor-specific or bus-wide.
""")


def main():
    args = parse_args()
    print_config(args)

    try:
        instrument = build_instrument(args)
    except Exception as e:
        print(f"\n[FATAL] Could not open serial port '{args.port}': {type(e).__name__}: {e}")
        print_open_port_diagnosis()
        sys.exit(1)

    last_exception = None
    for attempt in range(1, args.retries + 1):
        print(f"\n--- Attempt {attempt}/{args.retries} ---")
        try:
            registers = instrument.read_registers(
                args.start_register, args.count, functioncode=args.function_code
            )
            print(f"\n[SUCCESS] Read {len(registers)} register(s) from slave 0x{args.slave:02X}")
            print(f"  Raw values: {registers}")
            for i, raw in enumerate(registers):
                print(describe_register(args.start_register + i, raw))
            sys.exit(0)
        except minimalmodbus.NoResponseError as e:
            last_exception = e
            print(f"[FAIL] No response (timeout) -- zero bytes received back: {e}")
        except minimalmodbus.InvalidResponseError as e:
            last_exception = e
            print(f"[FAIL] Invalid response (likely CRC/framing error) -- bytes WERE received but didn't parse: {e}")
        except minimalmodbus.SlaveReportedException as e:
            last_exception = e
            print(f"[FAIL] Slave reported a Modbus exception (device IS responding, but rejected the request): {e}")
        except serial.SerialException as e:
            last_exception = e
            print(f"[FAIL] Serial port error: {e}")
        except Exception as e:
            last_exception = e
            print(f"[FAIL] Unexpected exception: {type(e).__name__}: {e}")

        if attempt < args.retries:
            time.sleep(args.retry_delay)

    print("\n" + "=" * 70)
    print(f"[RESULT] All {args.retries} attempt(s) failed.")
    print(f"[RESULT] Last exception: {type(last_exception).__name__}: {last_exception}")
    print("=" * 70)

    if args.raw_listen:
        raw_listen(args)

    print_diagnosis(last_exception)
    sys.exit(1)


if __name__ == "__main__":
    main()
