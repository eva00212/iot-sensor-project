#!/usr/bin/env bash
# verify_install.sh — PASS/FAIL/WARN report of every deployment prerequisite.
#
# Run after ./install.sh (and after any reboot it triggers) to confirm the
# Pi is actually ready to run sensor-collector, or to diagnose why a
# specific deployment isn't working. Read-only: this script never changes
# system state, only reports on it. See docs/DEPLOYMENT.md for what to do
# about each FAIL.
#
# Usage: ./verify_install.sh
# Exit code: 0 if everything passed (warnings are ok), 1 if anything failed.

set -uo pipefail
# Deliberately NOT `set -e` -- almost every check below is expected to
# sometimes "fail" (grep no match, command not found, etc.) as part of
# normal diagnostic control flow, not a script bug. Each is handled
# explicitly and reported, rather than aborting the whole report early.

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
SERVICE_NAME="sensor-collector"
SITE_CFG="$PROJECT_ROOT/config/site_config.yaml"

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

pass() { echo "  [PASS] $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  [FAIL] $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
warn() { echo "  [WARN] $1"; WARN_COUNT=$((WARN_COUNT + 1)); }
section() { echo ""; echo "== $1 =="; }

echo "=================================================================="
echo " sensor-collector deployment verification"
echo " $(date)"
echo "=================================================================="

# ── Hardware / OS ──────────────────────────────────────────────────────────────
section "Hardware / OS"

if [ -f /proc/device-tree/model ]; then
    MODEL="$(tr -d '\0' < /proc/device-tree/model)"
    if echo "$MODEL" | grep -q "Raspberry Pi 5"; then
        pass "Model: $MODEL"
    else
        warn "Model: $MODEL -- this project was built/verified against Raspberry Pi 5; other models may need adjustment"
    fi
else
    warn "Could not read /proc/device-tree/model -- is this actually a Raspberry Pi?"
fi

if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${VERSION_CODENAME:-}" = "bookworm" ]; then
        pass "OS: ${PRETTY_NAME:-bookworm}"
    else
        warn "OS: ${PRETTY_NAME:-unknown} -- Raspberry Pi 5 requires Bookworm or later; this may be an unsupported combination"
    fi
else
    warn "Could not read /etc/os-release"
fi

pass "Kernel: $(uname -r)"

if command -v rpi-eeprom-update >/dev/null 2>&1; then
    EEPROM_OUT="$(sudo rpi-eeprom-update 2>&1 || true)"
    if echo "$EEPROM_OUT" | grep -qi "UPDATE AVAILABLE"; then
        warn "Bootloader/EEPROM update available -- an outdated EEPROM is a known source of inconsistent behavior between otherwise-identical boards; see docs/DEPLOYMENT.md"
    else
        pass "Bootloader/EEPROM appears up to date"
    fi
else
    warn "rpi-eeprom-update not found -- cannot check bootloader/EEPROM version"
fi

# ── UART configuration ────────────────────────────────────────────────────────
section "UART configuration"

if [ -f /boot/firmware/config.txt ]; then
    CONFIG_TXT=/boot/firmware/config.txt
    CMDLINE_TXT=/boot/firmware/cmdline.txt
else
    CONFIG_TXT=/boot/config.txt
    CMDLINE_TXT=/boot/cmdline.txt
fi

if grep -qE '^\s*enable_uart=1\s*$' "$CONFIG_TXT" 2>/dev/null; then
    pass "enable_uart=1 present in $CONFIG_TXT"
else
    fail "enable_uart=1 NOT found in $CONFIG_TXT -- UART hardware not enabled (run ./install.sh)"
fi

if grep -qE 'console=(serial0|ttyAMA0|ttyS0)' "$CMDLINE_TXT" 2>/dev/null; then
    fail "A login console is still attached to the serial port in $CMDLINE_TXT -- it will interfere with Modbus communication (run ./install.sh)"
else
    pass "No login console attached to the serial port in $CMDLINE_TXT"
fi

if systemctl list-units --all --plain --no-legend 2>/dev/null | grep -E 'serial-getty@(ttyAMA0|ttyS0|serial0)\.service' | grep -q running; then
    fail "A serial-getty is actively running on the serial port -- it will interfere with Modbus communication"
else
    pass "No active serial-getty on the serial port"
fi

# ── Serial device ──────────────────────────────────────────────────────────────
section "Serial device (/dev/serial0)"

if [ -e /dev/serial0 ]; then
    SERIAL_TARGET="$(readlink -f /dev/serial0)"
    pass "/dev/serial0 exists -> $SERIAL_TARGET"
else
    fail "/dev/serial0 does not exist -- UART not enabled, or the Pi hasn't been rebooted since ./install.sh ran"
    SERIAL_TARGET=""
fi

if command -v pinctrl >/dev/null 2>&1; then
    PINMUX_OUT="$(pinctrl get 14,15 2>&1 || true)"
elif command -v raspi-gpio >/dev/null 2>&1; then
    PINMUX_OUT="$(raspi-gpio get 14,15 2>&1 || true)"
else
    PINMUX_OUT=""
fi

if [ -n "$PINMUX_OUT" ]; then
    if echo "$PINMUX_OUT" | grep -qiE 'uart|a0'; then
        pass "GPIO14/15 pinmux shows a UART function"
        echo "         $(echo "$PINMUX_OUT" | tr '\n' ' ')"
    else
        warn "GPIO14/15 pinmux doesn't clearly show a UART function -- got: $(echo "$PINMUX_OUT" | tr '\n' ' ')"
    fi
else
    warn "Neither 'pinctrl' nor 'raspi-gpio' available -- cannot check GPIO14/15 pinmux directly (run ./install.sh to install raspi-gpio)"
fi

# ── Required system packages ────────────────────────────────────────────────────
section "Required system packages"

for pkg in python3-venv python3-pip raspi-config; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        pass "Package installed: $pkg"
    else
        fail "Package missing: $pkg (run: sudo apt-get install -y $pkg)"
    fi
done

# ── Serial permissions ────────────────────────────────────────────────────────
section "Serial permissions"

if id -nG "$USER" 2>/dev/null | grep -qw dialout; then
    pass "Current session's groups include 'dialout'"
elif getent group dialout 2>/dev/null | grep -qw "$USER"; then
    warn "'$USER' is in the 'dialout' group in /etc/group, but not in this session -- log out and back in (or reboot) to pick it up. Not required for the systemd service itself, only for running scripts interactively as yourself."
else
    fail "'$USER' is not in the 'dialout' group (run: sudo usermod -aG dialout $USER, then log out/in)"
fi

if [ -n "${SERIAL_TARGET:-}" ] && [ -e "$SERIAL_TARGET" ]; then
    SERIAL_GROUP="$(stat -c '%G' "$SERIAL_TARGET" 2>/dev/null || echo unknown)"
    if [ "$SERIAL_GROUP" = "dialout" ]; then
        pass "$SERIAL_TARGET is owned by group 'dialout'"
    else
        warn "$SERIAL_TARGET is owned by group '$SERIAL_GROUP', expected 'dialout'"
    fi
fi

# ── Python environment ────────────────────────────────────────────────────────
section "Python environment"

if [ -x "$VENV_DIR/bin/python3" ]; then
    pass "Virtualenv exists at $VENV_DIR"
    for mod in yaml serial paho.mqtt.client sklearn minimalmodbus; do
        if "$VENV_DIR/bin/python3" -c "import $mod" >/dev/null 2>&1; then
            pass "Python module importable: $mod"
        else
            fail "Python module NOT importable: $mod (run: $VENV_DIR/bin/pip install -r requirements.txt)"
        fi
    done
else
    fail "Virtualenv not found at $VENV_DIR -- run ./install.sh"
fi

# ── Required directories ──────────────────────────────────────────────────────
section "Required directories"

for d in "$PROJECT_ROOT/logs" "$PROJECT_ROOT/models" "$VENV_DIR"; do
    if [ -d "$d" ] && [ -w "$d" ]; then
        pass "Exists and writable: $d"
    else
        fail "Missing or not writable: $d (run ./install.sh)"
    fi
done

AVAIL_KB="$(df -Pk "$PROJECT_ROOT" 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "${AVAIL_KB:-}" ]; then
    AVAIL_MB=$((AVAIL_KB / 1024))
    if [ "$AVAIL_MB" -lt 100 ]; then
        fail "Only ${AVAIL_MB}MB free on the filesystem holding $PROJECT_ROOT"
    elif [ "$AVAIL_MB" -lt 500 ]; then
        warn "Only ${AVAIL_MB}MB free on the filesystem holding $PROJECT_ROOT"
    else
        pass "${AVAIL_MB}MB free on the filesystem holding $PROJECT_ROOT"
    fi
else
    warn "Could not determine free disk space"
fi

# ── Site configuration ────────────────────────────────────────────────────────
section "Site configuration"

if [ -f "$SITE_CFG" ]; then
    pass "$SITE_CFG exists"
    if [ -x "$VENV_DIR/bin/python3" ]; then
        SITE_CHECK="$("$VENV_DIR/bin/python3" - "$SITE_CFG" <<'PYEOF' 2>&1
import sys
import yaml
with open(sys.argv[1], encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
missing = [] if cfg.get("site_id") else ["site_id"]
if not (cfg.get("server") or {}).get("host"):
    missing.append("server.host")
print("OK" if not missing else "MISSING:" + ",".join(missing))
PYEOF
)"
        case "$SITE_CHECK" in
            OK) pass "site_config.yaml has site_id and server.host set" ;;
            MISSING:*) fail "site_config.yaml is missing: ${SITE_CHECK#MISSING:}" ;;
            *) warn "Could not validate site_config.yaml contents: $SITE_CHECK" ;;
        esac
    fi
else
    fail "$SITE_CFG not found -- copy config/site_config.example.yaml to site_config.yaml and edit it"
fi

# ── systemd service ────────────────────────────────────────────────────────────
section "systemd service"

if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\.service"; then
    pass "Service unit installed: ${SERVICE_NAME}.service"
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        pass "Service enabled at boot"
    else
        fail "Service not enabled at boot (run: sudo systemctl enable $SERVICE_NAME)"
    fi
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        pass "Service is currently active"
    else
        warn "Service is not currently active (run: sudo systemctl start $SERVICE_NAME)"
    fi
else
    fail "Service unit not installed -- run ./install.sh"
fi

# ── Time synchronization ──────────────────────────────────────────────────────
section "Time synchronization"

if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q "yes"; then
        pass "System clock is NTP-synchronized"
    else
        warn "System clock is not (yet) NTP-synchronized -- most Pi boards have no hardware RTC, so timestamps can be wrong until NTP catches up over LTE"
    fi
else
    warn "timedatectl not available -- cannot check time sync status"
fi

# ── MQTT connectivity ──────────────────────────────────────────────────────────
section "MQTT connectivity"

if [ -f "$SITE_CFG" ] && [ -x "$VENV_DIR/bin/python3" ]; then
    MQTT_RESULT="$("$VENV_DIR/bin/python3" - "$SITE_CFG" <<'PYEOF' 2>&1
import sys
import time
import yaml
import paho.mqtt.client as mqtt

with open(sys.argv[1], encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
server = cfg.get("server") or {}
host = server.get("host")
port = server.get("port", 1883)

if not host:
    print("NO_HOST")
    sys.exit(0)

result = {"connected": None}

def on_connect(client, userdata, flags, reason_code, properties=None):
    result["connected"] = (reason_code == 0)
    client.disconnect()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="verify-install-check")
client.on_connect = on_connect
try:
    client.connect(host, port, keepalive=5)
    client.loop_start()
    deadline = time.monotonic() + 5
    while result["connected"] is None and time.monotonic() < deadline:
        time.sleep(0.1)
    client.loop_stop()
except Exception as e:
    print(f"ERROR:{e}")
    sys.exit(0)

if result["connected"] is True:
    print("OK")
elif result["connected"] is False:
    print("REJECTED")
else:
    print("TIMEOUT")
PYEOF
)"
    case "$MQTT_RESULT" in
        OK) pass "Connected to MQTT broker successfully" ;;
        NO_HOST) fail "site_config.yaml has no server.host set" ;;
        REJECTED) fail "MQTT broker rejected the connection (check credentials/client_id config)" ;;
        TIMEOUT) fail "Could not connect to MQTT broker within 5s (check LTE/network link, broker host/port, firewall)" ;;
        ERROR:*) fail "MQTT connection error: ${MQTT_RESULT#ERROR:}" ;;
        *) warn "Unexpected MQTT check result: $MQTT_RESULT" ;;
    esac
else
    warn "Skipping MQTT check -- site_config.yaml or virtualenv not found yet (run ./install.sh first)"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "=================================================================="
echo " Summary: $PASS_COUNT passed, $WARN_COUNT warning(s), $FAIL_COUNT failed"
echo "=================================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo " Result: FAIL -- see docs/DEPLOYMENT.md for how to resolve each item above."
    exit 1
fi
echo " Result: PASS"
exit 0
