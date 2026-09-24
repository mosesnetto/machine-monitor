#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="mcc_monitor.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0"
    exit 1
fi

echo "==> Setting up Python environment"
PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
    python3 -m venv "$DIR/.venv"
    "$DIR/.venv/bin/pip" install --quiet --upgrade pip
    "$DIR/.venv/bin/pip" install --quiet requests
    echo "    virtualenv created at $DIR/.venv"
else
    echo "    virtualenv already present"
fi

echo "==> Stopping any manual instance"
pkill -f "$DIR/mcc_monitor.py" 2>/dev/null || true
sleep 1

echo "==> Creating config file (edit to enable SIM real-call)"
ENV_FILE="$DIR/mcc_monitor.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# MCC Monitor optional settings. Uncomment/edit to override defaults.
# MCC_BREAKDOWN_ALERT_MIN=10
# MCC_CALL_ALERT_MIN=30
# Shift-end production report (sent X minutes before shift end)
# MCC_SHIFT_REPORT=1
# MCC_SHIFT_REPORT_MIN_BEFORE=2
# Call provider: auto|module|telegram
# MCC_CALL_PROVIDER=auto
# A7670E SIM module (real voice call via SIM; put in BREAKDOWN_CALL_TO)
# A7670_AT_PORT=/dev/ttyUSB1
# A7670_BAUD=115200
# A7670_AUDIO_DEVICE=default
# A7670_AUDIO_MAX_SECONDS=30
# A7670_RING_TIMEOUT_SECONDS=60
# To make a REAL phone call via the A7670 SIM module instead of the free Telegram voice message:
# BREAKDOWN_CALL_TO=+91xxxxxxxxxx
# BREAKDOWN_CALL_TEXT=MCC line 1 is in breakdown for more than 30 minutes, please check.
# Voice for the audible Telegram alert (Piper male voice by default):
# MCC_PIPER_BIN=/path/to/.venv_tts/bin/piper
# MCC_PIPER_MODEL=/path/to/voices/en_US-ryan-medium.onnx
# REQUIRED: put your Telegram bot token here (from @BotFather):
MCC_BOT_TOKEN=your_bot_token_here
EOF
    chmod 600 "$ENV_FILE"
    echo "    created $ENV_FILE (set BREAKDOWN_CALL_TO to enable real calls, then systemctl restart)"
fi

echo "==> Creating systemd unit $SERVICE_PATH"
cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=MCC Telegram Shift Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=-$ENV_FILE
ExecStart=$PY $DIR/mcc_monitor.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting service"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo ""
echo "Installed and started. Commands:"
echo "  systemctl status mcc_monitor     -- check status"
echo "  journalctl -u mcc_monitor -f     -- live logs"
echo "  sudo systemctl stop mcc_monitor  -- stop"
echo ""
echo "Log file: $DIR/mcc_monitor.log"