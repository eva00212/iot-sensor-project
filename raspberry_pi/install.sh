#!/usr/bin/env bash
# install.sh — one-command field deployment for a fresh Raspberry Pi OS
# (Bookworm/Trixie) install.
#
# Usage:
#   ./install.sh <site_id>              e.g. ./install.sh testBed06
#
# This is the recommended deployment path: clone the repo onto a freshly
# flashed Pi and run this script. It replaces the old "clone a golden SD
# card image, then hand-edit site_config.yaml" workflow. It is idempotent —
# safe to re-run (e.g. to change site_id, or after a `git pull`) without
# breaking an existing installation.

set -euo pipefail

# ── Args ──────────────────────────────────────────────────────────────────────
SITE_ID="${1:-}"

if [ -z "$SITE_ID" ]; then
    echo "Usage: $0 <site_id>   e.g. $0 testBed06" >&2
    exit 1
fi
if ! [[ "$SITE_ID" =~ ^testBed[0-9]{2}$ ]]; then
    echo "ERROR: site_id '$SITE_ID' doesn't match the required testBedNN format (e.g. testBed06)." >&2
    exit 1
fi

DEPLOY_USER="$(whoami)"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
SERVICE_NAME="sensor-collector"
SERVICE_SRC="$PROJECT_ROOT/service/${SERVICE_NAME}.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
POSTBOOT_NAME="sensor-collector-postboot"
POSTBOOT_SRC="$PROJECT_ROOT/service/${POSTBOOT_NAME}.service"
POSTBOOT_DST="/etc/systemd/system/${POSTBOOT_NAME}.service"
STATE_DIR="/etc/smartfarm"
PROVISIONED_MARKER="$STATE_DIR/.provisioned"
HOSTNAME_NEW="$(echo "$SITE_ID" | tr '[:upper:]' '[:lower:]')"

echo "==> Deploying site_id=$SITE_ID as user '$DEPLOY_USER' from $PROJECT_ROOT"
sudo mkdir -p "$STATE_DIR"

REBOOT_REQUIRED=0

# ── 0. Hardware check ─────────────────────────────────────────────────────────
# This project targets the Pi 5 specifically: its UART is behind the RP1 I/O
# controller rather than wired straight into the SoC like earlier models, so
# the config.txt syntax raspi-config needs to write differs from a Pi 3/4
# (dtparam=uart0 vs enable_uart). We don't hand-generate that config
# ourselves anywhere in this script — raspi-config's do_serial_hw already
# detects the board and branches accordingly — but we still flag a
# mismatched board early since RS485 timing/wiring assumptions elsewhere in
# this project (modbus_config.yaml) are Pi 5-specific too.
PI_MODEL="unknown"
if [ -r /proc/device-tree/model ]; then
    PI_MODEL="$(tr -d '\0' < /proc/device-tree/model)"
fi
echo "==> Detected hardware: $PI_MODEL"
case "$PI_MODEL" in
    *"Raspberry Pi 5"*) ;;
    *)
        echo "    WARNING: this script and this project are built for a Raspberry Pi 5."
        echo "    Detected '$PI_MODEL' instead — UART/RS485 behavior may differ." ;;
esac

# ── 1. System packages ────────────────────────────────────────────────────────
echo "==> Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip raspi-config

# ── 2. RS485 serial port access + UART enablement ─────────────────────────────
# The sensors are wired directly to this Pi's RS485 interface (default
# /dev/serial0, i.e. uart0). Reading it requires the deploy user in
# 'dialout' and the primary UART switched from "login console" to
# "hardware" mode — on a fresh Raspberry Pi OS install it defaults to the
# login console instead. raspi-config's nonint helpers write the right
# config.txt/cmdline.txt syntax for us — on a Pi 5 that's `dtparam=uart0`
# via RP1, different from the `enable_uart=1` used on earlier boards — and
# are idempotent by design. The kernel only picks the change up after a
# reboot though, so we check before/after state and only reboot if
# something actually changed.
echo "==> Checking RS485 serial port access..."
sudo usermod -aG dialout "$DEPLOY_USER"

HW_BEFORE="$(sudo raspi-config nonint get_serial_hw || echo 1)"
CONS_BEFORE="$(sudo raspi-config nonint get_serial_cons || echo 0)"

sudo raspi-config nonint do_serial_hw 0    # 0 = enable UART hardware
sudo raspi-config nonint do_serial_cons 1  # 1 = disable login shell over serial

if [ "$HW_BEFORE" != "0" ] || [ "$CONS_BEFORE" != "1" ]; then
    echo "    UART config changed — a reboot will be required to apply it."
    REBOOT_REQUIRED=1
else
    echo "    UART already configured for sensor access."
    if [ ! -e /dev/serial0 ]; then
        echo "    WARNING: /dev/serial0 still not present. Check wiring/config.txt manually."
    fi
fi

# ── 3. Python virtualenv + dependencies ───────────────────────────────────────
echo "==> Setting up virtualenv at $VENV_DIR..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
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

# ── 5. finalize_clone: site_id, hostname, machine-id, SSH host keys ─────────
finalize_clone() {
    # site_config.yaml — create from template if missing, then stamp site_id.
    local site_cfg="$PROJECT_ROOT/config/site_config.yaml"
    local site_cfg_example="$PROJECT_ROOT/config/site_config.example.yaml"
    if [ ! -f "$site_cfg" ]; then
        if [ -f "$site_cfg_example" ]; then
            echo "==> site_config.yaml not found - creating from example..."
            cp "$site_cfg_example" "$site_cfg"
        else
            echo "ERROR: neither $site_cfg nor $site_cfg_example exist." >&2
            exit 1
        fi
    fi
    sed -i "s|^site_id:.*|site_id: \"$SITE_ID\"|" "$site_cfg"
    echo "    site_id set to $SITE_ID in $site_cfg"

    # Hostname — applied immediately, no reboot needed.
    local current_hostname
    current_hostname="$(hostname)"
    if [ "$current_hostname" != "$HOSTNAME_NEW" ]; then
        echo "==> Setting hostname: $current_hostname -> $HOSTNAME_NEW"
        sudo hostnamectl set-hostname "$HOSTNAME_NEW"
        if grep -q "^127\.0\.1\.1" /etc/hosts; then
            sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
        else
            printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" | sudo tee -a /etc/hosts > /dev/null
        fi
    else
        echo "    Hostname already set to $HOSTNAME_NEW."
    fi

    # machine-id + SSH host keys — these only need regenerating once, the
    # first time a golden/cloned image is personalized into a real device.
    # Guarded by a marker file so re-running install.sh (to change site_id,
    # pull new code, etc.) never regenerates them again and invalidates
    # existing SSH known_hosts entries / D-Bus machine identity for no reason.
    if [ ! -f "$PROVISIONED_MARKER" ]; then
        echo "==> First-time provisioning: regenerating machine-id and SSH host keys..."
        sudo rm -f /etc/machine-id /var/lib/dbus/machine-id
        sudo systemd-machine-id-setup
        sudo ln -sf /etc/machine-id /var/lib/dbus/machine-id

        sudo rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
        sudo ssh-keygen -A > /dev/null
        sudo systemctl restart ssh 2>/dev/null || sudo systemctl restart sshd 2>/dev/null || true

        sudo touch "$PROVISIONED_MARKER"
        echo "    Done — this device now has a unique machine-id and SSH host keys."
    else
        echo "    machine-id / SSH host keys already provisioned, skipping."
    fi
}
finalize_clone

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "==> Installing systemd service..."

sed \
    -e "s|User=.*|User=$DEPLOY_USER|" \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_ROOT/src|" \
    -e "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/python3 $PROJECT_ROOT/src/collector.py|" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# ── 7. Postboot verification unit ─────────────────────────────────────────────
# Self-disabling oneshot: only needed when step 2 requires a reboot to apply
# UART changes. It runs verify_install.sh once after the reboot, logs the
# result to the journal, then disables itself so it never runs again.
sed \
    -e "s|__WORKDIR__|$PROJECT_ROOT|" \
    -e "s|__VERIFY_CMD__|$PROJECT_ROOT/verify_install.sh $SITE_ID|" \
    -e "s|__UNIT_NAME__|$POSTBOOT_NAME|" \
    "$POSTBOOT_SRC" | sudo tee "$POSTBOOT_DST" > /dev/null
sudo systemctl daemon-reload

if [ "$REBOOT_REQUIRED" -eq 1 ]; then
    sudo systemctl enable "$POSTBOOT_NAME"
    sudo systemctl restart "$SERVICE_NAME"
    echo ""
    echo "==> UART was just enabled for the first time — a reboot is required."
    echo "    Rebooting now. The service will start automatically on boot, and"
    echo "    verify_install.sh will run once to confirm everything came up"
    echo "    (check with: journalctl -u $POSTBOOT_NAME)."
    sleep 2
    sudo reboot
    exit 0
fi

# No reboot needed — start the service now and verify immediately.
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "==> Installation complete. Running verify_install.sh..."
echo ""
"$PROJECT_ROOT/verify_install.sh" "$SITE_ID"
