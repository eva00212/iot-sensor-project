#!/usr/bin/env bash
# test_prepare_master_image.sh
#
# Bash-level test for prepare_master_image.sh's file-handling logic --
# copies the real script into a throwaway sandbox directory (with fake
# logs/models/config files, no real systemd service involved) and
# verifies exactly what it removes and preserves. Safe to run anywhere;
# never touches the real project tree or any real system state.
#
# This is a *.sh test, not a *.py one -- it will NOT be picked up by
# `python -m unittest discover raspberry_pi/tests`. Run it directly:
#   bash raspberry_pi/tests/test_prepare_master_image.sh
#
# finalize_clone.sh is not covered by an equivalent test here: unlike
# this script, it performs genuinely system-level, non-reversible-in-a-
# sandbox operations (hostname, /etc/machine-id, SSH host keys) that
# can't be safely exercised without modifying a real system. Its
# testable logic (the site_config.yaml edit) is covered directly by
# test_update_site_id.py instead, since finalize_clone.sh calls that
# exact, tested code path rather than reimplementing it.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_SCRIPT="$SCRIPT_DIR/../prepare_master_image.sh"
SANDBOX="$(mktemp -d)"

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "  [PASS] $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  [FAIL] $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

if [ ! -f "$REAL_SCRIPT" ]; then
    echo "[FATAL] Could not find prepare_master_image.sh at $REAL_SCRIPT"
    exit 1
fi

echo "=================================================================="
echo " test_prepare_master_image.sh (sandbox: $SANDBOX)"
echo "=================================================================="

# ── Fixture setup ────────────────────────────────────────────────────────────
mkdir -p "$SANDBOX/logs" "$SANDBOX/models" "$SANDBOX/config"
cp "$REAL_SCRIPT" "$SANDBOX/prepare_master_image.sh"

echo '{"topic": "/multisensing/testBed01/device01", "body": "{}"}' > "$SANDBOX/logs/buffer.jsonl"
echo "some log line" > "$SANDBOX/logs/collector.log"
echo "rotated log line" > "$SANDBOX/logs/collector.log.1"
touch "$SANDBOX/models/testBed01_device01.pkl" "$SANDBOX/models/testBed01_device03.pkl"
SITE_CFG_CONTENT='site_id: "testBed01"
server:
  host: "example.com"'
echo "$SITE_CFG_CONTENT" > "$SANDBOX/config/site_config.yaml"

# ── Run 1 ────────────────────────────────────────────────────────────────────
echo ""
echo "== Run 1 (fresh state) =="
( cd "$SANDBOX" && bash prepare_master_image.sh > run1.log 2>&1 )
RUN1_EXIT=$?

if [ "$RUN1_EXIT" -eq 0 ]; then
    pass "Run 1 exits 0"
else
    fail "Run 1 exits 0 (got $RUN1_EXIT)"
fi

[ ! -e "$SANDBOX/logs/buffer.jsonl" ] && pass "buffer.jsonl removed" || fail "buffer.jsonl still present"
[ ! -e "$SANDBOX/logs/collector.log" ] && pass "collector.log removed" || fail "collector.log still present"
[ ! -e "$SANDBOX/logs/collector.log.1" ] && pass "collector.log.1 (rotated) removed" || fail "collector.log.1 still present"
[ ! -e "$SANDBOX/models/testBed01_device01.pkl" ] && pass "device01 model removed" || fail "device01 model still present"
[ ! -e "$SANDBOX/models/testBed01_device03.pkl" ] && pass "device03 model removed" || fail "device03 model still present"

if [ -f "$SANDBOX/config/site_config.yaml" ]; then
    if [ "$(cat "$SANDBOX/config/site_config.yaml")" = "$SITE_CFG_CONTENT" ]; then
        pass "site_config.yaml preserved byte-for-byte"
    else
        fail "site_config.yaml content changed"
    fi
else
    fail "site_config.yaml was removed (must be preserved)"
fi

if grep -q "not installed -- nothing to stop" "$SANDBOX/run1.log"; then
    pass "gracefully reports no systemd service to stop (sandbox has none)"
else
    fail "did not report the expected 'nothing to stop' message"
fi

if grep -q "Removed:" "$SANDBOX/run1.log"; then
    pass "prints what was removed"
else
    fail "did not print any 'Removed:' lines despite removing files"
fi

if grep -qi "ready to capture" "$SANDBOX/run1.log"; then
    pass "prints final 'ready to capture' confirmation"
else
    fail "did not print a final ready-to-capture confirmation"
fi

# ── Run 2 (idempotency) ──────────────────────────────────────────────────────
echo ""
echo "== Run 2 (already clean -- idempotency check) =="
( cd "$SANDBOX" && bash prepare_master_image.sh > run2.log 2>&1 )
RUN2_EXIT=$?

if [ "$RUN2_EXIT" -eq 0 ]; then
    pass "Run 2 (repeat run) exits 0"
else
    fail "Run 2 (repeat run) exits 0 (got $RUN2_EXIT)"
fi

if grep -qi "nothing to remove -- already clean" "$SANDBOX/run2.log"; then
    pass "Run 2 correctly reports nothing left to remove"
else
    fail "Run 2 did not report an already-clean state"
fi

if [ -f "$SANDBOX/config/site_config.yaml" ] && [ "$(cat "$SANDBOX/config/site_config.yaml")" = "$SITE_CFG_CONTENT" ]; then
    pass "site_config.yaml still preserved after Run 2"
else
    fail "site_config.yaml was affected by Run 2"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "=================================================================="
echo " $PASS_COUNT passed, $FAIL_COUNT failed"
echo "=================================================================="

[ "$FAIL_COUNT" -eq 0 ] && exit 0 || exit 1
