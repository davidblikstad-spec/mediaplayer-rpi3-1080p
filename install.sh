#!/usr/bin/env bash
# Installs the Media Player as a systemd service that owns the HDMI console.
# Run with: sudo ./install.sh
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo ./install.sh"
  exit 1
fi

echo "==> Installing system dependencies (GStreamer HW-decode stack + PyGObject)"
apt-get update
apt-get install -y \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-alsa \
  python3-gi python3-gi-cairo gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  ffmpeg

# The GStreamer Python bindings (gi) are system packages, so the app's venv
# must be allowed to see system site-packages.
PYVENV="$DIR/venv/pyvenv.cfg"
if [ -f "$PYVENV" ]; then
  if grep -q "include-system-site-packages" "$PYVENV"; then
    sed -i "s/^include-system-site-packages.*/include-system-site-packages = true/" "$PYVENV"
  else
    echo "include-system-site-packages = true" >> "$PYVENV"
  fi
  echo "    venv now sees system site-packages (for gi/GStreamer)"
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
