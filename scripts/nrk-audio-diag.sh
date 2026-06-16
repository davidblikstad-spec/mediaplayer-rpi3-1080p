#!/usr/bin/env bash
# Diagnose NRK live-stream audio dropouts in isolation: play NRK1 to the real
# HDMI audio device with video sent to a fakesink (so no DRM/console needed),
# capturing the audio sink's clock/slaving/underrun debug. Stops the mediaplayer
# service (to free the audio device) and ALWAYS restarts it. Run with sudo.
#
# Usage: sudo scripts/nrk-audio-diag.sh [METHOD]   (METHOD: resample|skew|none)
set -u
METHOD="${1:-resample}"
LOG=/tmp/nrk-audio-diag.log
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
trap 'echo "restarting service..."; systemctl start mediaplayer' EXIT

echo "stopping mediaplayer..."; systemctl stop mediaplayer; sleep 1

URL=$(/usr/bin/python3 - <<'PY'
import json, urllib.request as u
d = json.load(u.urlopen("https://psapi.nrk.no/playback/manifest/channel/nrk1", timeout=10))
print([a["url"] for a in d["playable"]["assets"] if a.get("url")][0])
PY
)
[ -n "${URL:-}" ] || { echo "could not resolve NRK url"; exit 1; }

echo "playing ~22s to HDMI with slave-method=$METHOD — LISTEN for drops..."
GST_DEBUG=2,audiobasesink:5 timeout -k3 22 \
  gst-launch-1.0 playbin3 uri="$URL" flags=0x13 video-sink=fakesink \
  audio-sink="alsasink device=sysdefault:CARD=vc4hdmi slave-method=$METHOD alignment-threshold=1000000000" \
  > "$LOG" 2>&1

echo "=== skew / resync / discont / underrun / writes ==="
grep -iaE "skew|resync|discont|underrun|xrun|correct|drift|crossed|recover" "$LOG" | tail -40
echo "(full log: $LOG, lines=$(wc -l < "$LOG"))"
