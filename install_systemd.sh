#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
SERVICE_USER="${1:-${SUDO_USER:-$(id -un)}}"
UNIT_PATH="/etc/systemd/system/ipsc2hbp.service"
if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./install_systemd.sh [service_user]" >&2
  exit 1
fi
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  echo "Missing venv/bin/python. Run ./setup_venv.sh before installing the service." >&2
  exit 1
fi
cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=IPSC to HomeBrew Protocol bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/ipsc2hbp.py -c $APP_DIR/ipsc2hbp.toml
Restart=on-failure
RestartSec=10s
TimeoutStopSec=10s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ipsc2hbp

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ipsc2hbp
echo "Installed and started ipsc2hbp.service as user $SERVICE_USER"
