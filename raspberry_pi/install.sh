#!/usr/bin/env bash
# install.sh — deploy the sensor collector on a fresh Raspberry Pi 5.
#
# Fully unattended after `git clone` + `cd raspberry_pi`:
#   ./install.sh
# The only thing left to do by hand afterward is editing the device-specific
# config/site_config.yaml (site_id, server settings) — everything else
# (system packages, venv, Python deps, UART enablement, systemd service,
# site_config.yaml scaffolding) is handled here.
#
# UART enablement requires a reboot to take effect. This script detects
# whether that's needed and, if so, reboots itself and resumes on the next
# run — so recovering a wiped Pi is just:
#   ./install.sh   (reboots partway through if this is a fresh OS image)
#   ./install.sh   (after the Pi comes back up, finishes the rest)

set -euo pipefail

DEPLOY_USER="$(whoami)"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
SERVICE_NAME="sensor-collector"
SERVICE_SRC="$PROJECT_ROOT/service/${SERVICE_NAME}.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

echo "==> Deploying as user '$DEPLOY_USER' from $PROJECT_ROOT"

# ── 1. System packages ────────────────────────────────────────────────────────
echo "==> Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip raspi-config

# ── 2. RS485 serial port: UART enable + dialout group ─────────────────────────
# The sensors are wired directly to this Pi's RS485 interface (default
# /dev/serial0). Reading it requires:
#   (a) the deploy user in the 'dialout' group, and
#   (b) the Pi's primary UART enabled for general use — on a fresh Raspberry
#       Pi OS install it's bound to the login console instead.
# Both are applied here via raspi-config's non-interactive mode, equivalent
# to: raspi-config -> Interface Options -> Serial Port ->
#     "login shell over serial?" No / "serial hardware enabled?" Yes
echo "==> Adding '$DEPLOY_USER' to the 'dialout' group..."
sudo usermod -aG dialout "$DEPLOY_USER"

echo "==> Configuring the UART for RS485 (serial hardware on, login console off)..."
# Boot config lives under /boot/firmware/ on current Raspberry Pi OS
# (Bookworm, required for the Pi 5); fall back to the legacy /boot/ path in
# case this ever runs on an older image.
if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG_FILES=(/boot/firmware/config.txt /boot/firmware/cmdline.txt)
else
    BOOT_CONFIG_FILES=(/boot/config.txt /boot/cmdline.txt)
fi

_boot_config_snapshot() {
    # Empty (but stable) output if the files don't exist yet, so a missing
    # file never crashes the snapshot under `set -e`.
    cat "${BOOT_CONFIG_FILES[@]}" 2>/dev/null | md5sum
}

BEFORE_SNAPSHOT="$(_boot_config_snapshot)"

sudo raspi-config nonint do_serial_cons 1   # disable login shell over serial
sudo raspi-config nonint do_serial_hw 0     # enable serial port hardware

AFTER_SNAPSHOT="$(_boot_config_snapshot)"

REBOOT_REQUIRED=0
if [ "$BEFORE_SNAPSHOT" != "$AFTER_SNAPSHOT" ]; then
    REBOOT_REQUIRED=1
elif [ ! -e /dev/serial0 ]; then
    # Config already matched (e.g. a previous run applied it) but the
    # device node still isn't there for some other reason -- safest to
    # still ask for a reboot rather than silently continuing.
    REBOOT_REQUIRED=1
fi

# Note: the 'dialout' group also covers most USB LTE modems' AT-command
# serial port, so no separate step is needed there. This script doesn't
# configure the LTE connection itself (ModemManager/ppp/ip routing are
# hardware-specific) — the service only requires that *some* interface
# eventually holds a default route; it doesn't care which one.

if [ "$REBOOT_REQUIRED" -eq 1 ]; then
    echo ""
    echo "=================================================================="
    echo " UART configuration changed -- a reboot is required before the"
    echo " serial port (/dev/serial0) will be usable."
    echo ""
    echo " After reboot, just run ./install.sh again from $PROJECT_ROOT --"
    echo " it will pick up where it left off and finish the rest of the"
    echo " setup (venv, dependencies, systemd service)."
    echo "=================================================================="
    echo ""

    ANSWER="y"
    if [ -t 0 ]; then
        read -t 15 -r -p "Reboot now? [Y/n] (auto-continuing in 15s) " ANSWER || true
    fi

    case "${ANSWER:-y}" in
        [nN]*)
            echo "==> Skipping reboot. Re-run ./install.sh after rebooting manually."
            exit 0
            ;;
        *)
            echo "==> Rebooting..."
            sudo reboot
            exit 0
            ;;
    esac
fi

# ── 3. Python virtualenv ──────────────────────────────────────────────────────
echo "==> Creating virtualenv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$PROJECT_ROOT/requirements.txt" -q
echo "    Done."

# ── 4. Remove stale model files ───────────────────────────────────────────────
MODELS_DIR="$PROJECT_ROOT/models"
if [ -d "$MODELS_DIR" ] && compgen -G "$MODELS_DIR/*.pkl" > /dev/null 2>&1; then
    echo "==> Removing stale .pkl models (will retrain automatically)..."
    rm -f "$MODELS_DIR"/*.pkl
fi
mkdir -p "$MODELS_DIR" "$PROJECT_ROOT/logs"

# ── 5. Site config ────────────────────────────────────────────────────────────
SITE_CFG="$PROJECT_ROOT/config/site_config.yaml"
SITE_CFG_EXAMPLE="$PROJECT_ROOT/config/site_config.example.yaml"
if [ ! -f "$SITE_CFG" ]; then
    if [ -f "$SITE_CFG_EXAMPLE" ]; then
        echo "==> site_config.yaml not found - copying from example..."
        cp "$SITE_CFG_EXAMPLE" "$SITE_CFG"
        echo "    *** Edit $SITE_CFG (including site_id) before starting the service. ***"
    else
        echo "WARNING: $SITE_CFG not found. Create it before starting the service."
    fi
fi

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "==> Installing systemd service..."

sed \
    -e "s|User=.*|User=$DEPLOY_USER|" \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_ROOT/src|" \
    -e "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/python3 $PROJECT_ROOT/src/collector.py|" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "==> Installation complete."
if [ ! -e /dev/serial0 ]; then
    echo "    NOTE: /dev/serial0 still not present -- the service will report"
    echo "    device_fault until it appears (see 'Sensor Fault' in the docs)."
fi
echo "    Service status: sudo systemctl status $SERVICE_NAME"
echo "    Live logs:      sudo journalctl -u $SERVICE_NAME -f"
