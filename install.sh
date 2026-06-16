#!/usr/bin/env bash
# Installs the Media Player as a systemd service that owns the HDMI console.
# Run with: sudo ./install.sh
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo ./install.sh"
  exit 1
fi

echo "==> Installing systemd unit"
install -m 644 "$DIR/mediaplayer.service" /etc/systemd/system/mediaplayer.service
systemctl daemon-reload

echo "==> Freeing tty1 (disabling text login on tty1 so the player owns HDMI)"
systemctl disable --now getty@tty1.service 2>/dev/null || true

echo "==> Enabling + starting mediaplayer"
systemctl enable mediaplayer.service
systemctl restart mediaplayer.service

sleep 3
systemctl --no-pager --lines=15 status mediaplayer.service || true

IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. Open the web interface at:  http://${IP:-<this-pi-ip>}:8080"
echo "First visit will ask you to create the admin username + password."
echo
echo "Logs:    journalctl -u mediaplayer -f"
echo "Restart: sudo systemctl restart mediaplayer"
