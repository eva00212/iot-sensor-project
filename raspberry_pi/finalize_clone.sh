#!/usr/bin/env bash
# finalize_clone.sh — run ONCE on each newly cloned Raspberry Pi, after its
# first boot, to give it its own unique identity before field deployment.
#
# Usage:
#   ./finalize_clone.sh testBed03
#
# What this does, in order (each step's outcome is tracked and reported in
# a final summary -- one failure doesn't hide the results of the rest):
#   1. Validates the site_id (must be testBed01..testBed08)
#   2. If this device was already finalized before, requires typed
#      confirmation before proceeding -- prevents accidentally re-using an
#      already-provisioned clone under a new identity by mistake
#   3. Updates config/site_config.yaml's site_id -- and ONLY that line, via
#      tools/update_site_id.py -- leaving every other setting (server
#      host, MQTT tuning, etc.) exactly as the master image had it
#   4. Sets the hostname to match (lowercased site_id)
#   5. Regenerates /etc/machine-id
#   6. Regenerates SSH host keys
#   7. Refreshes /var/lib/systemd/random-seed
#   8. Prints the resulting MQTT client_id and an example topic, computed
#      the same way server_uploader.py / onem2m_converter.py do at runtime
#   9. Enables and restarts sensor-collector
#  10. Runs ./verify_install.sh
#
# See docs/DEPLOYMENT.md for the full fleet-cloning workflow.

set -uo pipefail
# Deliberately not `set -e` -- each step below is tracked individually via
# step(), so one failure is reported clearly without hiding whether later
# steps also succeeded or failed. The final summary and exit code reflect
# overall success.

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python3"
SITE_CFG="$PROJECT_ROOT/config/site_config.yaml"
UPDATE_SITE_ID="$PROJECT_ROOT/tools/update_site_id.py"
MARKER="$PROJECT_ROOT/config/.finalized"
SERVICE_NAME="sensor-collector"

STEP_NAMES=()
STEP_RESULTS=()

step() {
    # step "<label>" <function-name>
    local label="$1"; shift
    if "$@"; then
        STEP_NAMES+=("$label"); STEP_RESULTS+=("OK")
        echo "  [OK]     $label"
    else
        STEP_NAMES+=("$label"); STEP_RESULTS+=("FAILED")
        echo "  [FAILED] $label"
    fi
}

# ── 0. Arguments ────────────────────────────────────────────────────────────
SITE_ID="${1:-}"
if [ -z "$SITE_ID" ]; then
    echo "Usage: $0 <site_id>   (e.g. $0 testBed03)"
    exit 1
fi
if [[ ! "$SITE_ID" =~ ^testBed0[1-8]$ ]]; then
    echo "[FATAL] '$SITE_ID' is not a valid site_id -- must be testBed01 through testBed08."
    exit 1
fi
HOSTNAME_NEW="$(echo "$SITE_ID" | tr '[:upper:]' '[:lower:]')"

echo "=================================================================="
echo " Finalizing this Raspberry Pi as: $SITE_ID (hostname: $HOSTNAME_NEW)"
echo "=================================================================="

if [ ! -f "$SITE_CFG" ]; then
    echo "[FATAL] $SITE_CFG not found -- run ./install.sh first (this doesn't"
    echo "        look like a clone of a properly prepared master image)."
    exit 1
fi

# ── 1. Prevent accidental reuse ─────────────────────────────────────────────
if [ -f "$MARKER" ]; then
    PREV_SITE_ID="$(grep '^site_id:' "$MARKER" 2>/dev/null | cut -d' ' -f2-)"
    PREV_TIME="$(grep '^timestamp:' "$MARKER" 2>/dev/null | cut -d' ' -f2-)"
    echo ""
    echo "*** WARNING: this device was already finalized. ***"
    echo "    Previous site_id : ${PREV_SITE_ID:-unknown}"
    echo "    Finalized at     : ${PREV_TIME:-unknown}"
    if [ "${PREV_SITE_ID:-}" = "$SITE_ID" ]; then
        echo "    You're re-running with the SAME site_id ($SITE_ID)."
    else
        echo "    You're now finalizing it as a DIFFERENT site_id ($SITE_ID) --"
        echo "    this may be the wrong device, or an intentional re-provision."
    fi
    echo ""
    if [ ! -t 0 ]; then
        echo "[FATAL] Not running in an interactive terminal -- refusing to"
        echo "        re-finalize an already-finalized device without confirmation."
        exit 1
    fi
    read -r -p "Type '$SITE_ID' again to confirm and proceed: " CONFIRM
    if [ "$CONFIRM" != "$SITE_ID" ]; then
        echo "Confirmation did not match -- aborting. Nothing was changed."
        exit 1
    fi
    echo ""
fi

echo "== Applying identity =="

# ── 2. site_config.yaml (surgical edit, preserves everything else) ─────────
_update_site_config() {
    if [ -x "$VENV_PYTHON" ]; then
        "$VENV_PYTHON" "$UPDATE_SITE_ID" "$SITE_CFG" "$SITE_ID" > /dev/null
    else
        python3 "$UPDATE_SITE_ID" "$SITE_CFG" "$SITE_ID" > /dev/null
    fi
}
step "config/site_config.yaml: site_id -> $SITE_ID (other settings preserved)" _update_site_config

# Write the reuse-prevention marker as soon as the device has an assigned
# identity -- only once that step actually succeeded, since a marker
# claiming an identity that wasn't actually written would be misleading.
if [ "${STEP_RESULTS[-1]}" = "OK" ]; then
    {
        echo "site_id: $SITE_ID"
        echo "timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo "hostname: $HOSTNAME_NEW"
    } > "$MARKER"
fi

# ── 3. Hostname ──────────────────────────────────────────────────────────────
_set_hostname() {
    sudo raspi-config nonint do_hostname "$HOSTNAME_NEW" || return 1
    return 0
}
step "hostname -> $HOSTNAME_NEW" _set_hostname

# ── 4. machine-id ────────────────────────────────────────────────────────────
_regen_machine_id() {
    sudo rm -f /etc/machine-id || return 1
    sudo systemd-machine-id-setup || return 1
    # /var/lib/dbus/machine-id is a symlink to /etc/machine-id on current
    # Raspberry Pi OS; regenerate it directly too in case it's ever a real
    # file instead (older/non-standard setups).
    if [ -e /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then
        sudo cp /etc/machine-id /var/lib/dbus/machine-id || return 1
    fi
    return 0
}
step "/etc/machine-id regenerated" _regen_machine_id

# ── 5. SSH host keys ─────────────────────────────────────────────────────────
_regen_ssh_keys() {
    sudo rm -f /etc/ssh/ssh_host_* || return 1
    sudo ssh-keygen -A > /dev/null || return 1
    if systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.service'; then
        sudo systemctl restart ssh || return 1
    elif systemctl list-unit-files 2>/dev/null | grep -q '^sshd\.service'; then
        sudo systemctl restart sshd || return 1
    fi
    # New host keys apply to new connections only -- this does not drop
    # the current SSH session, if you're running this remotely.
    return 0
}
step "SSH host keys regenerated" _regen_ssh_keys

# ── 6. random-seed ───────────────────────────────────────────────────────────
_refresh_random_seed() {
    sudo rm -f /var/lib/systemd/random-seed
    sudo systemd-random-seed save > /dev/null 2>&1 || true
    return 0
}
step "/var/lib/systemd/random-seed refreshed" _refresh_random_seed

# ── 7. Confirm MQTT identity derivation ─────────────────────────────────────
echo ""
echo "== MQTT identity (derived automatically from the new site_id) =="
if [ -x "$VENV_PYTHON" ]; then
    "$VENV_PYTHON" - "$SITE_CFG" "$SITE_ID" <<'PYEOF'
import sys
import yaml

site_cfg_path, site_id = sys.argv[1], sys.argv[2]
with open(site_cfg_path, encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
base_client_id = (cfg.get("server") or {}).get("client_id", "rpi-uploader")
print(f"  Effective MQTT client_id : {base_client_id}-{site_id}")
print(f"  Example MQTT topic       : /multisensing/{site_id}/device01")
PYEOF
else
    echo "  (venv not found -- skipping; client_id will be '<server.client_id>-$SITE_ID')"
fi

# ── 8. systemd service ────────────────────────────────────────────────────────
echo ""
echo "== Service =="
_restart_service() {
    sudo systemctl enable "$SERVICE_NAME" || return 1
    sudo systemctl restart "$SERVICE_NAME" || return 1
    return 0
}
step "sensor-collector enabled + restarted" _restart_service

# ── 9. Verification ──────────────────────────────────────────────────────────
echo ""
echo "== Verification =="
_run_verify() {
    "$PROJECT_ROOT/verify_install.sh"
}
step "verify_install.sh" _run_verify

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "=================================================================="
echo " Finalize summary for $SITE_ID"
echo "=================================================================="
FAILED=0
for i in "${!STEP_NAMES[@]}"; do
    printf "  [%s] %s\n" "${STEP_RESULTS[$i]}" "${STEP_NAMES[$i]}"
    [ "${STEP_RESULTS[$i]}" = "FAILED" ] && FAILED=1
done
echo "=================================================================="

if [ "$FAILED" -eq 1 ]; then
    echo " Result: FAILED -- one or more steps above need attention before"
    echo "         this device is ready for field deployment."
    exit 1
fi
echo " Result: SUCCESS -- this Pi is finalized as $SITE_ID and ready for"
echo "         field deployment."
exit 0
