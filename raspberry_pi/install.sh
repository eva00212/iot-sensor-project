#!/usr/bin/env bash
# install.sh — deploy the sensor collector on a fresh Raspberry Pi 5

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
sudo apt-get install -y python3-venv python3-pip

# ── 2. RS485 serial port access ───────────────────────────────────────────────
# The sensors are wired directly to this Pi's RS485 interface (default
# /dev/serial0). Reading it requires:
#   (a) the deploy user in the 'dialout' group, and
#   (b) the Pi's primary UART enabled for general use — on a fresh Raspberry
#       Pi OS install it's usually bound to the login console instead. Enable
#       it with: sudo raspi-config -> Interface Options -> Serial Port ->
#       "login shell over serial? No" / "serial hardware enabled? Yes"
#       (or edit /boot/firmware/config.txt + /boot/firmware/cmdline.txt
#       directly), then reboot.
echo "==> Checking RS485 serial port access..."
sudo usermod -aG dialout "$DEPLOY_USER"
if [ ! -e /dev/serial0 ]; then
    echo "    WARNING: /dev/serial0 not found. Enable the UART via raspi-config"
    echo "    (Interface Options -> Serial Port) and reboot before starting the service."
fi
# Note: the 'dialout' group also covers most USB LTE modems' AT-command
# serial port, so no separate step is needed there. This script doesn't
# configure the LTE connection itself (ModemManager/ppp/ip routing are
# hardware-specific) — the service only requires that *some* interface
# eventually holds a default route; it doesn't care which one.

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

DEPLOY_HOME="$(eval echo ~"$DEPLOY_USER")"
VENV_PYTHON="$DEPLOY_HOME/$(realpath --relative-to="$HOME" "$VENV_DIR")/bin/python3"
WORK_DIR="$DEPLOY_HOME/$(realpath --relative-to="$HOME" "$PROJECT_ROOT/src")"
SCRIPT="$DEPLOY_HOME/$(realpath --relative-to="$HOME" "$PROJECT_ROOT/src/collector.py")"

sed \
    -e "s|User=.*|User=$DEPLOY_USER|" \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$WORK_DIR|" \
    -e "s|ExecStart=.*|ExecStart=$VENV_DIR/bin/python3 $PROJECT_ROOT/src/collector.py|" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "==> Installation complete."
echo "    Service status: sudo systemctl status $SERVICE_NAME"
echo "    Live logs:      sudo journalctl -u $SERVICE_NAME -f"
