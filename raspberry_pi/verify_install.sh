#!/usr/bin/env bash
# verify_install.sh — sanity-check a sensor-collector deployment.
#
# Usage: ./verify_install.sh [site_id]
#
# Run automatically at the end of install.sh (directly, or via the
# postboot unit when a UART reboot was required). Safe to run by hand any
# time to check the health of an existing install. Exits non-zero if any
# check marked FAIL fails; WARN checks (e.g. missing hardware, useful when
# testing with simulator.py) don't affect the exit code.

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
SITE_CFG="$PROJECT_ROOT/config/site_config.yaml"
SERVICE_NAME="sensor-collector"
EXPECTED_SITE_ID="${1:-}"

PASS=0
FAIL=0
WARN=0

check_pass() { echo "  [PASS] $1"; PASS=$((PASS+1)); }
check_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
check_warn() { echo "  [WARN] $1"; WARN=$((WARN+1)); }

echo "==> Verifying sensor-collector installation at $PROJECT_ROOT"
echo ""

# ── Hardware ───────────────────────────────────────────────────────────────────
echo "-- Hardware model --"
PI_MODEL="unknown"
if [ -r /proc/device-tree/model ]; then
    PI_MODEL="$(tr -d '\0' < /proc/device-tree/model)"
fi
if [[ "$PI_MODEL" == *"Raspberry Pi 5"* ]]; then
    check_pass "running on $PI_MODEL"
else
    check_warn "expected a Raspberry Pi 5, detected '$PI_MODEL' — UART/RS485 behavior may differ"
fi

# ── Virtualenv + dependencies ─────────────────────────────────────────────────
echo "-- Python environment --"
if [ -x "$VENV_DIR/bin/python3" ]; then
    check_pass "virtualenv exists ($VENV_DIR)"
else
    check_fail "virtualenv missing or broken at $VENV_DIR"
fi

if [ -x "$VENV_DIR/bin/python3" ]; then
    if "$VENV_DIR/bin/python3" -c "import paho.mqtt.client, yaml, sklearn, minimalmodbus" 2>/dev/null; then
        check_pass "required Python packages importable"
    else
        check_fail "one or more required Python packages failed to import"
    fi
fi

# ── Config ─────────────────────────────────────────────────────────────────────
echo "-- Configuration --"
if [ -f "$SITE_CFG" ]; then
    check_pass "site_config.yaml exists"
    ACTUAL_SITE_ID="$("$VENV_DIR/bin/python3" -c "import yaml; print(yaml.safe_load(open('$SITE_CFG'))['site_id'])" 2>/dev/null || echo "")"
    if [[ "$ACTUAL_SITE_ID" =~ ^testBed[0-9]{2}$ ]]; then
        check_pass "site_id is valid ($ACTUAL_SITE_ID)"
    else
        check_fail "site_id missing or malformed in site_config.yaml"
    fi
    if [ -n "$EXPECTED_SITE_ID" ] && [ "$ACTUAL_SITE_ID" != "$EXPECTED_SITE_ID" ]; then
        check_fail "site_id ($ACTUAL_SITE_ID) does not match expected ($EXPECTED_SITE_ID)"
    fi
else
    check_fail "site_config.yaml not found"
fi

for f in modbus_config.yaml rule_config.yaml; do
    if [ -f "$PROJECT_ROOT/config/$f" ]; then
        check_pass "$f present"
    else
        check_fail "$f missing"
    fi
done

# ── Hardware / UART ────────────────────────────────────────────────────────────
echo "-- Hardware --"
if [ -e /dev/serial0 ]; then
    check_pass "/dev/serial0 present (UART enabled)"
else
    check_warn "/dev/serial0 not found — UART not enabled, or no reboot yet. RS485 polling will fail until this is fixed (fine if only testing with simulator.py)."
fi

if id -nG "$(whoami)" | grep -qw dialout; then
    check_pass "current user is in the 'dialout' group"
else
    check_warn "current user is not in 'dialout' — expected if verifying as a different user than the service runs as"
fi

# ── Service ────────────────────────────────────────────────────────────────────
echo "-- systemd service --"
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    check_pass "$SERVICE_NAME is enabled at boot"
else
    check_fail "$SERVICE_NAME is not enabled"
fi

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    check_pass "$SERVICE_NAME is active"
else
    check_fail "$SERVICE_NAME is not active (check: journalctl -u $SERVICE_NAME -e)"
fi

# ── Identity ───────────────────────────────────────────────────────────────────
echo "-- Device identity --"
CURRENT_HOSTNAME="$(hostname)"
if [ -n "$EXPECTED_SITE_ID" ]; then
    EXPECTED_HOSTNAME="$(echo "$EXPECTED_SITE_ID" | tr '[:upper:]' '[:lower:]')"
    if [ "$CURRENT_HOSTNAME" = "$EXPECTED_HOSTNAME" ]; then
        check_pass "hostname matches site_id ($CURRENT_HOSTNAME)"
    else
        check_fail "hostname ($CURRENT_HOSTNAME) does not match expected ($EXPECTED_HOSTNAME)"
    fi
else
    check_pass "hostname is $CURRENT_HOSTNAME"
fi

if [ -s /etc/machine-id ]; then
    check_pass "machine-id present ($(cat /etc/machine-id))"
else
    check_fail "/etc/machine-id missing or empty"
fi

echo ""
echo "==> $PASS passed, $WARN warning(s), $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
