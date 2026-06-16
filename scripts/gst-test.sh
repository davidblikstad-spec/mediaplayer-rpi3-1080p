#!/usr/bin/env bash
# Direct hardware-overlay playback test, bypassing mpv AND our app.
# GStreamer decodes H.264 on the Pi's V4L2 codec block and scans the frame out
# via kmssink onto a DRM hardware plane (the vc4 HVS) — the path that should give
# smooth 1080p if the hardware can. Run with sudo. Stops the mediaplayer service,
# runs the pipeline on seat0/tty1, prints fps/dropped, then ALWAYS restores it.
#
# Usage: sudo scripts/gst-test.sh [FILE]
set -u
FILE="${1:-/home/david/mediaplayer/media/01._Origo_Solutions_Grand_Opening_4K_1.mp4}"
UNIT=gsttest
LOG=/tmp/gst-test.log
PIPE_SINK="${SINK:-kmssink}"   # override with SINK=... for variants

cleanup() {
  echo "--- cleanup: restoring mediaplayer service ---"
  systemctl stop "$UNIT" 2>/dev/null
  systemctl reset-failed "$UNIT" 2>/dev/null
  systemctl start mediaplayer
  echo "mediaplayer restarted."
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
exec > >(tee /tmp/gst-test-result.txt) 2>&1
command -v gst-launch-1.0 >/dev/null || {
  echo "GStreamer not installed. Run:"
  echo "  sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad"
  exit 1; }
[ -f "$FILE" ] || { echo "file not found: $FILE"; exit 1; }

echo "=== GStreamer HW decode + $PIPE_SINK (DRM hardware plane) ==="
echo "file: $FILE"
systemctl stop mediaplayer
sleep 2
: > "$LOG"

# v4l2h264dec = Pi hardware H.264 decoder; fpsdisplaysink wraps the real sink and
# reports rendered/dropped frame counts so we get an objective smoothness number.
systemd-run --unit="$UNIT" --collect \
  -p User=david -p PAMName=login -p TTYPath=/dev/tty1 \
  -p StandardInput=tty -p "StandardOutput=append:$LOG" -p "StandardError=append:$LOG" \
  -p "SupplementaryGroups=video render input audio tty" \
  --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
  gst-launch-1.0 filesrc location="$FILE" ! qtdemux ! h264parse ! v4l2h264dec ! \
     fpsdisplaysink video-sink="$PIPE_SINK" text-overlay=false sync=true silent=false

echo "playing ~16s — WATCH THE TV for smoothness..."
sleep 16

echo "=== fps / dropped (fpsdisplaysink; want fps≈25, dropped≈0) ==="
grep -oE "rendered: [0-9]+, dropped: [0-9]+, current: [0-9.-]+, average: [0-9.-]+" "$LOG" | tail -8
echo "=== pipeline state / errors ==="
grep -iE "ERROR|not-negotiated|fail|no element|cannot|missing|WARNING|Setting pipeline" "$LOG" | grep -viE "GST_DEBUG" | tail -10
echo "=== done (service restored on exit) ==="
