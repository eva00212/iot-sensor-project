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
  python test_modbus_sensor.py --scan   # try slave 1-10 x {2400,4800,9600,19200} baud, register 505
  python test_modbus_sensor.py --self-test    # no hardware -- verify frame bytes vs. the manual
  python test_modbus_sensor.py --raw-probe    # bypass minimalmodbus, send the manual's exact frame
  python test_modbus_sensor.py --rts-direction   # add to any mode above if DE/RE is wired to RTS
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


DEFAULT_SCAN_BAUDRATES = [2400, 4800, 9600, 19200]
DEFAULT_SCAN_SLAVES = "1-10"


def to_signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


def parse_int_list(spec: str) -> list:
    """'2400,4800,9600' -> [2400, 4800, 9600]"""
    return [int(x.strip(), 0) for x in spec.split(",") if x.strip()]


def parse_int_ranges(spec: str) -> list:
    """'1-10' or '1,2,5' or '1-3,7,9-10' -> sorted unique ints."""
    values = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            values.update(range(int(lo, 0), int(hi, 0) + 1))
        else:
            values.add(int(part, 0))
    return sorted(values)


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
    p.add_argument("--raw-probe", action="store_true",
                    help="Bypass minimalmodbus entirely: send the manual's exact wind-speed "
                         "request frame (01 03 01 F4 00 01 C4 04) via raw pyserial, wait at "
                         "least 1s, and print every received byte in hex with timing. Runs "
                         "instead of the normal minimalmodbus-based read.")
    p.add_argument("--self-test", action="store_true",
                    help="No hardware required: verify minimalmodbus builds byte-exact request "
                         "frames for the manual's two worked examples (register 500 count 1, "
                         "register 504 count 2), independent of any real serial port. Runs "
                         "instead of the normal read.")
    p.add_argument("--rts-direction", action="store_true",
                    help="Enable pyserial's kernel-level RS485 mode (RTS toggled exactly around "
                         "each transmission) for boards where DE/RE is wired to RTS rather than "
                         "having its own auto-direction sensing circuit. Off by default.")
    p.add_argument("--quiet", action="store_true",
                    help="Disable minimalmodbus's own verbose TX/RX byte logging")

    p.add_argument("--scan", action="store_true",
                    help="Auto-scan mode: try every slave address x baud rate combination "
                         "(reading --start-register/--count/--function-code at each) instead "
                         "of testing a single --slave/--baudrate. Useful when the sensor's "
                         "actual address or baud rate is unknown/uncertain.")
    p.add_argument("--scan-slaves", default=DEFAULT_SCAN_SLAVES,
                    help="Slave addresses to try in --scan mode, e.g. '1-10' or '1,2,5'")
    p.add_argument("--scan-baudrates", default=",".join(str(b) for b in DEFAULT_SCAN_BAUDRATES),
                    help="Comma-separated baud rates to try in --scan mode")
    p.add_argument("--scan-timeout", type=float, default=0.3,
                    help="Per-attempt timeout in --scan mode (kept short since most of the "
                         "40 combinations are expected to fail)")
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


def apply_serial_settings(ser, args, timeout: float) -> None:
    """Applies the exact port settings the manual requires (4800 8N1, no flow
    control), explicitly rather than relying on pyserial defaults, and
    optionally enables kernel-level RS485 direction control."""
    ser.baudrate = args.baudrate
    ser.bytesize = args.bytesize
    ser.parity = PARITY_MAP[args.parity]
    ser.stopbits = args.stopbits
    ser.timeout = timeout
    ser.xonxoff = False   # no software flow control
    ser.rtscts = False    # no RTS/CTS hardware flow control
    ser.dsrdtr = False    # no DSR/DTR hardware flow control

    if args.rts_direction:
        # For RS485 boards where DE/RE is tied to RTS rather than having its
        # own auto-direction sensing circuit: the kernel driver asserts RTS
        # (or clears it, depending on rts_level_for_tx) exactly around the
        # actual UART transmission, timed off real hardware TX-empty state
        # -- more reliable than toggling a GPIO from Python around write().
        import serial.rs485
        ser.rs485_mode = serial.rs485.RS485Settings(
            rts_level_for_tx=True,
            rts_level_for_rx=False,
        )


def build_instrument(args):
    inst = minimalmodbus.Instrument(args.port, args.slave, mode=minimalmodbus.MODE_RTU)
    apply_serial_settings(inst.serial, args, args.timeout)
    inst.close_port_after_each_call = False
    inst.debug = not args.quiet  # minimalmodbus prints raw TX/RX bytes itself when True
    return inst


def raw_listen(args, duration=2.0):
    print()
    print(f"[RAW LISTEN] Opening {args.port} directly for {duration}s to check for ANY incoming bytes")
    print("[RAW LISTEN] (this bypasses Modbus framing/CRC entirely -- pure wire-level check)")
    try:
        ser = serial.Serial(port=None)  # don't open yet -- configure first
        ser.port = args.port
        apply_serial_settings(ser, args, duration)
        with ser:
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


def check_port_openable(port: str) -> Exception | None:
    """Quick check that `port` can be opened at all, independent of Modbus.
    Returns the exception on failure, or None if it opened fine."""
    try:
        with serial.Serial(port=port, timeout=0.1):
            pass
        return None
    except Exception as e:
        return e


def _scan_attempt(port: str, slave: int, baud: int, args):
    """Single scan attempt: returns the register list on success, None on any failure.
    Uses close_port_after_each_call so each attempt gets a clean connection at the
    baud rate under test, rather than reusing a stale cached connection."""
    try:
        inst = minimalmodbus.Instrument(port, slave, mode=minimalmodbus.MODE_RTU)
        apply_serial_settings(inst.serial, args, args.scan_timeout)
        inst.serial.baudrate = baud  # override: this is what's actually under test here
        inst.close_port_after_each_call = True
        inst.debug = False
        return inst.read_registers(args.start_register, args.count, functioncode=args.function_code)
    except Exception:
        return None


def run_scan(args) -> bool:
    """Runs the full slave x baudrate scan. Returns True if at least one
    combination responded successfully."""
    slaves = parse_int_ranges(args.scan_slaves)
    baudrates = parse_int_list(args.scan_baudrates)

    print("=" * 70)
    print("Modbus auto-scan mode")
    print("=" * 70)
    print(f"  Port           : {args.port}")
    print(f"  Frame          : {args.bytesize}{args.parity}{args.stopbits}")
    fc_name = "Read Holding Registers" if args.function_code == 3 else "Read Input Registers"
    print(f"  Function code  : 0x{args.function_code:02X} ({fc_name})")
    print(f"  Register(s)    : start={args.start_register}, count={args.count}")
    print(f"  Slave addresses: {slaves}")
    print(f"  Baud rates     : {baudrates}")
    print(f"  Per-attempt timeout: {args.scan_timeout}s")
    total = len(slaves) * len(baudrates)
    print(f"  Total combinations: {total} (worst case ~{total * args.scan_timeout:.0f}s if none respond)")
    print("=" * 70)

    port_error = check_port_openable(args.port)
    if port_error is not None:
        print(f"\n[FATAL] Could not open serial port '{args.port}': {type(port_error).__name__}: {port_error}")
        print("[FATAL] Aborting scan -- this is a port-level problem, not an address/baud issue.")
        print_open_port_diagnosis()
        return False

    hits = []
    attempted = 0
    start_time = time.time()

    for baud in baudrates:
        for slave in slaves:
            attempted += 1
            result = _scan_attempt(args.port, slave, baud, args)
            if result is not None:
                print(f"[{attempted:>3}/{total}] baud={baud:<6} slave=0x{slave:02X} ({slave:>2})  -> SUCCESS  raw={result}")
                hits.append((baud, slave, result))
            else:
                print(f"[{attempted:>3}/{total}] baud={baud:<6} slave=0x{slave:02X} ({slave:>2})  -> fail")

    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print(f"Scan complete in {elapsed:.1f}s -- {len(hits)} responsive combination(s) found")
    print("=" * 70)

    if hits:
        print()
        for baud, slave, regs in hits:
            print(f"  MATCH: slave=0x{slave:02X} ({slave}), baud={baud}, raw={regs}")
            for i, raw in enumerate(regs):
                print(describe_register(args.start_register + i, raw))
        baud, slave, _ = hits[0]
        print("\nRe-run against this combination directly for full diagnostics:")
        print(f"  python {sys.argv[0]} --port {args.port} --slave 0x{slave:02X} --baudrate {baud} "
              f"--start-register {args.start_register} --count {args.count}")
    else:
        print("\nNo responsive slave address / baud rate combination found in the range tried.")
        print("This suggests the problem is likely NOT just a wrong address or baud rate --")
        print("see the wiring/power/DE-RE diagnostic steps below.")
        print_diagnosis(None)

    return bool(hits)


# Manual's exact worked examples, used by --self-test and --raw-probe.
MANUAL_WIND_SPEED_FRAME = bytes.fromhex("01 03 01 F4 00 01 C4 04".replace(" ", ""))       # slave 1, func 3, reg 500, count 1
MANUAL_HUMIDITY_TEMP_FRAME = bytes.fromhex("01 03 01 F8 00 02 44 06".replace(" ", ""))    # slave 1, func 3, reg 504, count 2


class _CapturingFakeSerial:
    """Minimal stand-in for serial.Serial that only records what gets
    written to it and never returns a response. Used by --self-test to
    verify minimalmodbus's exact request-frame bytes with no real port,
    cable, or sensor involved at all."""

    def __init__(self):
        self.is_open = True
        self.in_waiting = 0
        self.timeout = 0.05
        self.baudrate = 4800
        self.bytesize = 8
        self.parity = "N"
        self.stopbits = 1
        self.port = "FAKE"
        self.last_written = b""

    def write(self, data):
        self.last_written = bytes(data)
        return len(data)

    def read(self, size=1):
        return b""

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def close(self):
        pass

    def open(self):
        pass


def run_self_test() -> bool:
    """Verifies minimalmodbus builds byte-exact request frames for the
    manual's two worked examples. No real serial port, cable, or sensor
    involved -- this isolates frame/CRC/addressing correctness from
    anything hardware-related."""
    print("=" * 70)
    print("Self-test: request frame bytes vs. the manual's worked examples")
    print("(no hardware involved -- pure software verification)")
    print("=" * 70)

    cases = [
        ("wind speed (500, count 1)",    500, 1, MANUAL_WIND_SPEED_FRAME),
        ("humidity+temp (504, count 2)", 504, 2, MANUAL_HUMIDITY_TEMP_FRAME),
    ]

    all_ok = True
    for name, reg, count, expected in cases:
        fake = _CapturingFakeSerial()
        inst = minimalmodbus.Instrument(fake, 1, mode=minimalmodbus.MODE_RTU)
        try:
            inst.read_registers(reg, count, functioncode=3)
        except minimalmodbus.NoResponseError:
            pass  # expected -- the fake never returns a response

        got = fake.last_written
        ok = got == expected
        all_ok = all_ok and ok
        print(f"\n[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"  Expected (manual): {expected.hex(' ').upper()}")
        print(f"  Transmitted:       {got.hex(' ').upper()}")
        if not ok:
            print("  !! MISMATCH -- this would indicate a bug in this script's frame")
            print("  !! construction, register addressing, or CRC handling. Investigate")
            print("  !! the software side before doing any hardware troubleshooting.")

    print("\n" + "=" * 70)
    if all_ok:
        print("[RESULT] All frame bytes match the manual exactly.")
        print("[RESULT] Register addressing (protocol addresses 500/504, not PLC-style")
        print("[RESULT] 40501/40506, and no off-by-one), CRC16 generation, and function")
        print("[RESULT] code framing are confirmed correct at the software level. Any")
        print("[RESULT] remaining communication failure is very likely hardware, wiring,")
        print("[RESULT] or direction-control related -- not a Modbus framing bug here.")
    else:
        print("[RESULT] At least one frame did NOT match the manual. Investigate this")
        print("[RESULT] script / minimalmodbus version before assuming a hardware fault.")
    print("=" * 70)
    return all_ok


def run_raw_probe(args, wait_seconds: float = 1.0) -> None:
    """Bypasses minimalmodbus entirely: opens the port directly, sends the
    manual's exact wind-speed request frame, waits at least `wait_seconds`,
    and prints every byte received with arrival timing. Use this to see
    exactly what the sensor (or the bus) sends back, independent of any
    Modbus-level parsing or CRC validation."""
    poll_timeout = 0.05  # short per-read timeout so the deadline loop below controls total wait precisely

    print("=" * 70)
    print("Raw probe mode (bypasses minimalmodbus entirely)")
    print("=" * 70)
    print(f"  Port           : {args.port}")
    print(f"  Frame          : {args.bytesize}{args.parity}{args.stopbits}, no flow control")
    print(f"  Baud rate      : {args.baudrate}")
    print(f"  Request frame  : {MANUAL_WIND_SPEED_FRAME.hex(' ').upper()}  (manual's wind-speed example)")
    print(f"  Wait time      : {wait_seconds}s (manual requires >= 200ms poll/response spacing)")
    print("=" * 70)

    try:
        ser = serial.Serial(port=None)  # don't open yet -- configure first
        ser.port = args.port
        apply_serial_settings(ser, args, poll_timeout)
        with ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            t0 = time.monotonic()
            n_written = ser.write(MANUAL_WIND_SPEED_FRAME)
            ser.flush()  # block until the OS has handed all bytes to the UART
            t_send_done = time.monotonic()
            print(f"\n[SEND] Wrote {n_written} byte(s): {MANUAL_WIND_SPEED_FRAME.hex(' ').upper()}")
            print(f"[SEND] flush() returned after {t_send_done - t0:.4f}s")

            print(f"[WAIT] Listening for {wait_seconds}s...")
            received = bytearray()
            first_byte_at = None
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                chunk = ser.read(1)
                if chunk:
                    if first_byte_at is None:
                        first_byte_at = time.monotonic() - t_send_done
                    received += chunk

            elapsed = time.monotonic() - t_send_done
            print(f"\n[RESULT] Listened for {elapsed:.3f}s after send (>= {wait_seconds}s requested).")

            if received:
                print(f"[RESULT] Received {len(received)} byte(s), first byte arrived after {first_byte_at:.4f}s:")
                print(f"  {bytes(received).hex(' ').upper()}")

                if bytes(received) == MANUAL_WIND_SPEED_FRAME:
                    print("\n  !! The received bytes are an EXACT ECHO of what was sent.")
                    print("  !! This strongly suggests the RS485 transceiver is looping the")
                    print("  !! transmitted signal straight back into the receiver -- typically")
                    print("  !! because DE and RE end up simultaneously asserted (driver and")
                    print("  !! receiver both enabled at once), rather than the request actually")
                    print("  !! reaching the sensor and a real reply coming back. Check DE/RE")
                    print("  !! direction control before assuming the sensor itself is dead.")
                elif first_byte_at is not None and first_byte_at < 0.005:
                    print("\n  !! First byte arrived in under 5ms -- too fast to be a real sensor")
                    print("  !! turnaround. Likely a local echo rather than a genuine reply;")
                    print("  !! check DE/RE direction control (see --rts-direction).")
            else:
                print("[RESULT] Received 0 bytes -- nothing came back at all.")
                print("[RESULT] Since --self-test already confirms the transmitted frame is")
                print("[RESULT] byte-exact correct, this points at wiring, power, sensor")
                print("[RESULT] address/baud, or DE/RE direction control -- not at the Modbus")
                print("[RESULT] frame content.")
    except Exception as e:
        print(f"\n[FATAL] Raw probe failed to open/use '{args.port}': {type(e).__name__}: {e}")
        print_open_port_diagnosis()


def warn_if_below_manual_floor(args) -> None:
    """The manual requires >= 200ms between poll/response cycles. Warn (but
    don't block) if the user has configured something faster than that."""
    floor = 0.2
    checks = [("--timeout", getattr(args, "timeout", None)),
              ("--scan-timeout", getattr(args, "scan_timeout", None))]
    for name, value in checks:
        if value is not None and value < floor:
            print(f"[WARNING] {name}={value}s is below the manual's 200ms minimum "
                  f"poll/response spacing requirement.")


def main():
    args = parse_args()
    warn_if_below_manual_floor(args)

    if args.self_test:
        sys.exit(0 if run_self_test() else 1)

    if args.scan:
        success = run_scan(args)
        sys.exit(0 if success else 1)

    if args.raw_probe:
        run_raw_probe(args)
        sys.exit(0)

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
