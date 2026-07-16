#!/usr/bin/env python3
"""
change_slave_id.py

Standalone provisioning tool: changes an RS485 Modbus RTU sensor's slave
address via Modbus function code 0x06 (Write Single Register).

Reuses this project's verified raw-pyserial Modbus transport directly --
imports modbus_poller.py and calls its actual _crc16_modbus, _get_serial,
and _read_registers_once rather than reimplementing or copying them, and
does not use minimalmodbus (see modbus_poller.py's own module docstring
for why this project doesn't use that library). modbus_poller.py itself
is NOT modified: it stays a read-only driver for the production
collector, and the write capability added here is scoped entirely to
this standalone, human-supervised tool.

*** CAUTION: register 2000 (0x07D0, the default below) is NOT yet
*** confirmed by the sensor's manual to be the slave-address register.
*** It was identified empirically -- an FC06 write to it was echoed back
*** exactly as a valid Modbus write should be. That echo only proves the
*** write was ACCEPTED at the protocol level. A Modbus RTU slave will
*** echo a valid FC06 write to *any* writable register, whether or not
*** that register actually controls the slave address -- so the echo
*** alone does not prove register 2000 is correct. That's why every
*** write here requires interactive confirmation and is followed by a
*** mandatory read-back verification pass (does the sensor now respond
*** at the NEW address, and does it stop responding at the OLD one?)
*** before this tool reports success. Confirm you have a recovery plan
*** (factory reset, or physical access to re-scan/re-wire) before
*** running this on a sensor you can't easily get back to a known state.

Usage:
  python change_slave_id.py --current 1 --new 5
  python change_slave_id.py --current 1 --new 5 --register 0x07D0
  python change_slave_id.py --current 1 --new 5 --port /dev/ttyUSB0 --baud 4800
  python change_slave_id.py --current 1 --new 5 --yes          # skip confirmation prompt
  python change_slave_id.py --current 1 --new 5 --dry-run      # build/print the frame, don't send it
"""

import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import serial

import modbus_poller  # reused directly: _crc16_modbus, _get_serial, _read_registers_once, ModbusError, config

WRITE_SINGLE_REGISTER = 0x06
SLAVE_ID_REGISTER_DEFAULT = 0x07D0  # 2000 decimal -- UNVERIFIED, see module docstring above

VALID_SLAVE_RANGE = range(1, 248)  # Modbus RTU unicast address range (0 is broadcast, 248-255 reserved)

READ_BACK_ATTEMPTS = 3       # small local retry for the post-write verification reads --
READ_BACK_RETRY_DELAY = 0.3  # separate from modbus_poller's production retry settings,
                              # which are tuned for the collector's poll cadence, not a
                              # one-shot interactive check


def _build_write_request(slave_addr: int, register: int, value: int) -> bytes:
    """Modbus FC06 (Write Single Register) request frame. Same shape as
    modbus_poller._build_request (addr, func, param1, param2, then CRC)
    -- reuses the same _crc16_modbus, just with function code 0x06
    instead of 0x03 and a register value instead of a read quantity."""
    body = struct.pack(">BBHH", slave_addr, WRITE_SINGLE_REGISTER, register, value)
    crc = modbus_poller._crc16_modbus(body)
    return body + struct.pack("<H", crc)


def _write_single_register(slave_addr: int, register: int, value: int) -> bytes:
    """Sends an FC06 write and returns the raw response bytes on success.
    Raises modbus_poller.ModbusError on a short/garbled response or a
    Modbus exception reply, mirroring _read_registers_once's own
    validation (short-response check, exception-code check, CRC check) --
    same rigor, applied to a write response instead of a read response."""
    ser = modbus_poller._get_serial()
    request = _build_write_request(slave_addr, register, value)

    ser.reset_input_buffer()
    ser.write(request)
    ser.flush()

    response = ser.read(len(request))

    if len(response) < 5:
        raise modbus_poller.ModbusError(
            f"No/short response from slave {slave_addr:#04x} "
            f"({len(response)} bytes) -- write may not have been received"
        )

    addr, func = response[0], response[1]
    if func & 0x80:
        raise modbus_poller.ModbusError(
            f"Slave {addr:#04x} rejected the write: exception code {response[2]:#04x} "
            f"for function {func & 0x7F:#04x} (register {register:#06x} may not be writable)"
        )

    if len(response) == len(request):
        payload, recv_crc = response[:-2], response[-2:]
        calc_crc = modbus_poller._crc16_modbus(payload)
        recv_crc_val = recv_crc[0] | (recv_crc[1] << 8)
        if calc_crc != recv_crc_val:
            raise modbus_poller.ModbusError(
                f"CRC mismatch on write response: calculated {calc_crc:#06x}, "
                f"received {recv_crc_val:#06x}, frame {response.hex()}"
            )

    return response


def _verify_echo(request: bytes, response: bytes) -> bool:
    return request == response


def _try_read(slave_addr: int, attempts: int = READ_BACK_ATTEMPTS) -> bool:
    """One-shot-per-attempt read (not modbus_poller's production retry
    path) against a known-good register (humidity, populated by every
    device variant) -- used purely to answer "does anything answer at
    this address", not to fetch a real measurement."""
    for _ in range(attempts):
        try:
            modbus_poller._read_registers_once(slave_addr, modbus_poller.REG_HUMIDITY, 1)
            return True
        except (modbus_poller.ModbusError, serial.SerialException, OSError):
            time.sleep(READ_BACK_RETRY_DELAY)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Change an RS485 Modbus RTU sensor's slave address (FC06 write to an "
                     "UNVERIFIED candidate register by default -- see this script's module docstring).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--current", type=lambda x: int(x, 0), required=True,
                         help="Sensor's CURRENT slave address (decimal or 0x-hex)")
    parser.add_argument("--new", type=lambda x: int(x, 0), required=True,
                         help="NEW slave address to assign (decimal or 0x-hex, 1-247)")
    parser.add_argument("--register", type=lambda x: int(x, 0), default=SLAVE_ID_REGISTER_DEFAULT,
                         help="Slave-address holding register to write (default: 2000 / 0x07D0, UNVERIFIED)")
    parser.add_argument("--port", default=modbus_poller.SERIAL_PORT, help="Serial port device")
    parser.add_argument("--baud", type=int, default=modbus_poller.BAUDRATE, help="Baud rate")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt")
    parser.add_argument("--dry-run", action="store_true", help="Build and print the write frame, but don't send it")
    args = parser.parse_args()

    if args.current not in VALID_SLAVE_RANGE:
        print(f"[FATAL] --current {args.current} is outside the valid Modbus RTU unicast range (1-247)")
        sys.exit(1)
    if args.new not in VALID_SLAVE_RANGE:
        print(f"[FATAL] --new {args.new} is outside the valid Modbus RTU unicast range (1-247)")
        sys.exit(1)
    if args.current == args.new:
        print(f"[FATAL] --current and --new are both {args.current} -- nothing to change")
        sys.exit(1)

    # Reused config, overridable for ad-hoc bench testing without touching
    # modbus_config.yaml -- these are read by name (not passed as
    # parameters) inside modbus_poller._get_serial(), so overriding the
    # module attributes before the first call takes effect correctly.
    modbus_poller.SERIAL_PORT = args.port
    modbus_poller.BAUDRATE = args.baud

    print("=" * 70)
    print("Slave address change (FC06 Write Single Register)")
    print("=" * 70)
    print(f"  Port             : {args.port}")
    print(f"  Baud rate        : {args.baud}")
    print(f"  Current address  : {args.current:#04x} ({args.current})")
    print(f"  New address      : {args.new:#04x} ({args.new})")
    print(f"  Target register  : {args.register:#06x} ({args.register})")
    print("=" * 70)
    print()
    print("*** CAUTION ***")
    print(f"Register {args.register:#06x} is NOT yet confirmed by the sensor's manual to")
    print("control the slave address. It was identified empirically: an FC06 write to")
    print("it was echoed back, which only confirms the write was ACCEPTED -- not that")
    print("it changed the address specifically. This tool verifies the real outcome by")
    print("reading back from the sensor at the NEW address afterward (see below), but a")
    print("valid echo on the wrong register could still leave the sensor in an")
    print("unexpected state. Confirm you have a way to recover (factory reset / physical")
    print("re-scan) before proceeding on a sensor you can't easily re-address.")
    print()

    request = _build_write_request(args.current, args.register, args.new)
    print(f"Write frame to send: {request.hex(' ').upper()}")

    if args.dry_run:
        print("\n[DRY RUN] Not sending. Re-run without --dry-run to actually write.")
        sys.exit(0)

    if not args.yes:
        answer = input(
            f"\nProceed with writing address {args.new} to slave {args.current} "
            f"at register {args.register:#06x}? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            sys.exit(1)

    try:
        response = _write_single_register(args.current, args.register, args.new)
    except (modbus_poller.ModbusError, serial.SerialException, OSError) as e:
        print(f"\n[FAIL] Write failed: {e}")
        sys.exit(1)

    print(f"Response received : {response.hex(' ').upper()}")
    if _verify_echo(request, response):
        print("[OK] Response echoes the write request exactly (protocol-level success).")
    else:
        print("[WARN] Response does not byte-match the request -- inspect the bytes above")
        print("       manually before trusting anything changed.")

    # ── Read-back verification ──────────────────────────────────────────────
    # The echo above only proves the write was accepted -- it does not prove
    # register `args.register` actually controls the slave address. The only
    # real confirmation is: does the sensor now respond at the NEW address,
    # and does it stop responding at the OLD one?
    print()
    print("Verifying by reading from the sensor at both addresses...")
    time.sleep(0.5)  # give the sensor a moment in case it needs to apply the change

    responds_at_new = _try_read(args.new)
    responds_at_old = _try_read(args.current)

    print(f"  Responds at NEW address {args.new:#04x}: {'YES' if responds_at_new else 'no'}")
    print(f"  Responds at OLD address {args.current:#04x}: {'YES' if responds_at_old else 'no'}")

    if responds_at_new and not responds_at_old:
        print("\n[SUCCESS] The sensor now responds only at the new address -- the change")
        print(f"          took effect, and register {args.register:#06x} does appear to be")
        print("          the slave-address register on this unit. Update")
        print("          modbus_config.yaml / this device's wiring documentation")
        print("          accordingly, and consider this register CONFIRMED (not just")
        print("          empirically observed) for this sensor model going forward.")
        sys.exit(0)
    elif responds_at_new and responds_at_old:
        print("\n[UNCLEAR] The sensor responds at BOTH addresses. This could mean another")
        print("          device on the bus is using the old address, or the write had an")
        print("          unexpected side effect. Do not assume success -- disconnect other")
        print("          devices on the bus and re-run the check before relying on this.")
        sys.exit(1)
    elif not responds_at_new and responds_at_old:
        print("\n[FAILURE] The sensor still responds at the OLD address only -- the address")
        print(f"          did not change. Register {args.register:#06x} is likely NOT the")
        print("          slave-address register on this unit, despite accepting the write.")
        sys.exit(1)
    else:
        print("\n[FAILURE] The sensor doesn't respond at EITHER address. Check wiring/power")
        print("          before assuming the address is lost -- it may just be a bus issue.")
        print("          If it truly no longer responds, consult the sensor manual for a")
        print("          factory-reset procedure.")
        sys.exit(1)


if __name__ == "__main__":
    main()
