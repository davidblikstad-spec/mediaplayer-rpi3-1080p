#!/usr/bin/env bash
# Objectively measure NRK live-audio dropouts on the real HDMI sink: negotiated
# audio caps, QoS/late-buffer drops, and alsasink's own dropped-sample stat over
# time. Stops the mediaplayer service to free the device and ALWAYS restarts it.
# Usage: sudo scripts/nrk-audio-probe.sh
set -u
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
REPO=/home/david/mediaplayer
trap 'echo; echo "restarting mediaplayer..."; systemctl start mediaplayer' EXIT
echo "stopping mediaplayer..."; systemctl stop mediaplayer; sleep 1

"$REPO/venv/bin/python" -u - <<'PY'
import time, json, os, urllib.request as U
import gi; gi.require_version("Gst","1.0")
from gi.repository import Gst, GLib
Gst.init(None)
url=[a["url"] for a in json.load(U.urlopen("https://psapi.nrk.no/playback/manifest/channel/nrk1",timeout=10))["playable"]["assets"] if a.get("url")][0]
pb=Gst.ElementFactory.make("playbin3"); pb.set_property("uri",url); pb.set_property("flags",0x13)
pb.set_property("video-sink",Gst.ElementFactory.make("fakesink"))
asink=Gst.ElementFactory.make("alsasink"); asink.set_property("device","plughw:CARD=vc4hdmi,DEV=0")
pb.set_property("audio-sink",asink)
bus=pb.get_bus(); bus.add_signal_watch(); t0=time.time()
def on(b,m):
    et=time.time()-t0
    if m.type==Gst.MessageType.QOS:
        try: fmt,proc,drop=m.parse_qos_stats()
        except Exception: proc=drop=-1
        try: live,rt,st,ts,dur=m.parse_qos()
        except Exception: live=False
        print("%6.2f QOS from %-12s processed=%s dropped=%s live=%s"%(et,m.src.get_name(),proc,drop,live),flush=True)
    elif m.type==Gst.MessageType.WARNING:
        print("%6.2f WARN %s: %s"%(et,m.src.get_name(),m.parse_warning()[0].message),flush=True)
    elif m.type==Gst.MessageType.ERROR:
        print("%6.2f ERROR %s"%(et,m.parse_error()[0].message),flush=True)
bus.connect("message",on)
pb.set_state(Gst.State.PLAYING)
def tick():
    et=time.time()-t0
    pad=asink.get_static_pad("sink"); caps=pad.get_current_caps()
    try: st=asink.get_property("stats").to_string()
    except Exception: st="?"
    print("%6.2f caps=%s | stats=%s"%(et, caps.to_string()[:70] if caps else None, st),flush=True)
    return True
GLib.timeout_add_seconds(2, tick)
loop=GLib.MainLoop()
GLib.timeout_add_seconds(26, lambda:(loop.quit(),False)[1])
loop.run(); pb.set_state(Gst.State.NULL); print("done"); os._exit(0)
PY
