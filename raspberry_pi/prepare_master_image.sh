#!/usr/bin/env bash
# prepare_master_image.sh — run on the verified master Raspberry Pi
# immediately before creating the SD card image that will be cloned to
# other devices.
#
# Removes this device's own accumulated runtime state (undelivered MQTT
# buffer, log files, trained AI models) so the captured image starts
# clean for every clone, instead of every clone inheriting the master's
# specific history. Deliberately does NOT touch:
#   - project code
#   - the Python virtualenv (.venv)
#   - UART / config.txt / cmdline.txt settings
#   - the installed systemd service
#   - config/site_config.yaml (this device's own identity -- if this
#     master Pi is itself one of the deployed units, it needs to keep
#     running with its own valid config; only the OTHER clones get a new
#     site_id, via finalize_clone.sh, after imaging)
#
# Safe to run more than once -- every removal is a no-op if the file is
# already gone.
#
# See docs/DEPLOYMENT.md for the full fleet-cloning workflow.

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="sensor-collector"

shopt -s nullglob

REMOVED=()

echo "=================================================================="
echo " Preparing master image: $PROJECT_ROOT"
echo "=================================================================="

# ── 1. Stop the service safely ──────────────────────────────────────────────
echo ""
echo "== Service =="
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\.service"; then
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo "==> Stopping $SERVICE_NAME..."
        sudo systemctl stop "$SERVICE_NAME"
        echo "    Stopped."
    else
        echo "==> $SERVICE_NAME already stopped."
    fi
else
    echo "==> $SERVICE_NAME not installed -- nothing to stop."
fi

# ── 2. Remove runtime state ─────────────────────────────────────────────────
echo ""
echo "== Removing accumulated runtime state =="

BUFFER="$PROJECT_ROOT/logs/buffer.jsonl"
if [ -f "$BUFFER" ]; then
    rm -f "$BUFFER"
    REMOVED+=("$BUFFER")
fi

for f in "$PROJECT_ROOT"/logs/collector.log*; do
    rm -f "$f"
    REMOVED+=("$f")
done

for f in "$PROJECT_ROOT"/models/*.pkl; do
    rm -f "$f"
    REMOVED+=("$f")
done

if [ "${#REMOVED[@]}" -eq 0 ]; then
    echo "  (nothing to remove -- already clean)"
else
    for f in "${REMOVED[@]}"; do
        echo "  Removed: $f"
    done
fi

# ── 3. Confirm what was preserved ───────────────────────────────────────────
echo ""
echo "== Preserved (untouched) =="
echo "  Project code, .venv/, UART config, systemd service unit"
SITE_CFG="$PROJECT_ROOT/config/site_config.yaml"
if [ -f "$SITE_CFG" ]; then
    CURRENT_SITE_ID="$(grep '^site_id:' "$SITE_CFG" | head -1 || true)"
    echo "  config/site_config.yaml (this device's own identity: ${CURRENT_SITE_ID:-not set})"
else
    echo "  config/site_config.yaml: NOT PRESENT (run ./install.sh before imaging if this is unexpected)"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "=================================================================="
if [ "${#REMOVED[@]}" -eq 0 ]; then
    echo " Master image ready to capture (already clean)."
else
    echo " Master image ready to capture -- ${#REMOVED[@]} file(s) removed above."
fi
echo ""
echo " Next steps:"
echo "   1. Power off this Pi:   sudo systemctl poweroff"
echo "   2. Image its SD card with your cloning tool of choice"
echo "   3. Clone that image onto each additional SD card"
echo "   4. On EACH cloned Pi (not this master), run:"
echo "        ./finalize_clone.sh <site_id>"
echo "      to give it its own unique identity before deployment."
echo "=================================================================="
