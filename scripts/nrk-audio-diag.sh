#!/usr/bin/env bash
# Find an HDMI ALSA device string that plays NRK live audio cleanly. Plays NRK1
# audio through several candidate devices in turn (video -> fakesink, so no
# DRM/console needed); LISTEN and note which segment(s) are smooth vs dropping.
# Stops the mediaplayer service to free the audio device and ALWAYS restarts it.
#
# Usage: sudo scripts/nrk-audio-diag.sh [DEVICE]
#   no arg  -> cycle through candidate devices (~9s each)
#   DEVICE  -> test just that one (e.g. "plughw:CARD=vc4hdmi,DEV=0")
set -u
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
trap 'echo; echo "restarting mediaplayer..."; systemctl start mediaplayer' EXIT

echo "stopping mediaplayer..."; systemctl stop mediaplayer; sleep 1
URL=$(/usr/bin/python3 - <<'PY'
import json, urllib.request as u
d = json.load(u.urlopen("https://psapi.nrk.no/playback/manifest/channel/nrk1", timeout=10))
print([a["url"] for a in d["playable"]["assets"] if a.get("url")][0])
PY
)
[ -n "${URL:-}" ] || { echo "could not resolve NRK url"; exit 1; }

if [ $# -ge 1 ]; then
  DEVICES=("$1")
else
  DEVICES=(
    "plughw:CARD=vc4hdmi,DEV=0"
    "hdmi:CARD=vc4hdmi,DEV=0"
    "default:CARD=vc4hdmi"
    "sysdefault:CARD=vc4hdmi"
  )
fi

i=0
for DEV in "${DEVICES[@]}"; do
  i=$((i+1))
  echo
  echo "############################################################"
  echo "### TEST $i/${#DEVICES[@]}: $DEV"
  echo "### LISTEN NOW for ~9s — is it smooth or dropping?"
  echo "############################################################"
  GST_DEBUG=1 timeout -k3 9 gst-launch-1.0 -q playbin3 uri="$URL" flags=0x13 \
    video-sink=fakesink audio-sink="alsasink device=$DEV" >/tmp/nrk-dev.log 2>&1
  if grep -qiE "Unknown PCM|No such device|could not open|cannot find card" /tmp/nrk-dev.log; then
    echo ">>> $DEV : FAILED TO OPEN CLEANLY (see error)"
    grep -iE "Unknown PCM|No such|could not open|cannot find" /tmp/nrk-dev.log | head -2
  else
    echo ">>> $DEV : opened ok"
  fi
  sleep 1
done
echo
echo "Done. Tell me which TEST number(s) sounded smooth."
