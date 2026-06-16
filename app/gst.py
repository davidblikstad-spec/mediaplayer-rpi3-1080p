"""GStreamer playback backend: hardware-decoded, zero-copy DRM-plane video.

Replaces the former mpv/JSON-IPC backend. On the Pi 3 the path that actually
plays 1080p smoothly is the V4L2 hardware H.264 decoder (v4l2h264dec, rank
primary+1 so it auto-plugs) feeding a DRM hardware plane via kmssink — the
zero-copy scanout that mpv's vo=drm overlay could not do (its atomic commit was
rejected by vc4 with EINVAL). GStreamer's kmssink performs that commit, proven
by scripts/gst-test.sh, so we drive a GStreamer pipeline directly via PyGObject.

  * Video + audio: playbin3 with video-sink=kmssink. playbin gives us native
    volume, seeking (used for in/out trim), position/duration queries and EOS.
  * Still images: a small `... ! imagefreeze ! videoconvert ! kmssink` pipeline
    held for the item's display duration.

Only one pipeline holds the DRM plane at a time; the other is forced to NULL
(which releases it) before the active one starts. End-of-stream on the bus is
surfaced to listeners as {"event": "end-file", "reason": "eof"} — the exact
contract PlayerEngine consumed from mpv, so the playlist engine is unchanged in
spirit.
"""
import os
import subprocess
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from . import config, media  # noqa: E402

Gst.init(None)


class GstPlayer:
    """Owns the GStreamer pipelines and exposes mpv-style playback primitives."""

    def __init__(self, log=print):
        self.log = log
        self.event_handlers = []          # list of callables(event_dict)
        self._lock = threading.RLock()
        self.playbin = None               # reused playbin3 for video/audio
        self.imgpipe = None               # per-image pipeline (rebuilt each time)
        self._active_bus = None           # bus the watcher polls
        self._gen = 0                     # cancels stale image timers
        self._img_timer = None
        self._cur_path = None             # source file currently shown
        self._cur_start = 0.0             # trim-in offset, for screenshots
        self._cur_kind = None
        self._volume = 100                # last requested volume (0..100)
        self._paused = False
        self._stop = False
        self._watcher = None

    # ---- lifecycle --------------------------------------------------------
    def start(self):
        self._stop = False
        self._build_playbin()
        self._watcher = threading.Thread(target=self._watch_bus, daemon=True)
        self._watcher.start()

    def _build_playbin(self):
        pb = Gst.ElementFactory.make("playbin3", "player")
        if pb is None:
            self.log("playbin3 unavailable")
            return
        vsink = Gst.ElementFactory.make("kmssink", "vsink")
        if vsink is not None:
            pb.set_property("video-sink", vsink)
        # no on-screen subtitles/visualisations
        self.playbin = pb

    def restart(self):
        """Tear down and rebuild the pipelines (e.g. after a wedged sink)."""
        with self._lock:
            self._stop_video()
            self._stop_image()
            if self.playbin is not None:
                self.playbin.set_state(Gst.State.NULL)
            self._build_playbin()

    def is_alive(self):
        return self.playbin is not None

    # ---- loading / playback ----------------------------------------------
    def load(self, path, *, kind, start=0.0, end=None, image_dur=None):
        """Show `path`. kind: 'image' freezes a frame for image_dur seconds;
        anything else plays as video/audio, optionally trimmed to [start, end]."""
        with self._lock:
            self._cancel_image_timer()
            self._cur_path = path
            self._cur_start = float(start or 0.0)
            self._cur_kind = kind
            self._paused = False
            if kind == "image":
                self._stop_video()
                self._play_image(path, image_dur)
            else:
                self._stop_image()
                self._play_video(path, self._cur_start, end)

    def _play_video(self, path, start, end):
        pb = self.playbin
        if pb is None:
            return
        pb.set_state(Gst.State.READY)            # flush any previous stream
        pb.set_property("uri", Gst.filename_to_uri(path))
        pb.set_state(Gst.State.PAUSED)
        pb.get_state(5 * Gst.SECOND)             # wait for preroll
        if start > 0 or end is not None:
            flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
            stop_type = Gst.SeekType.SET if end is not None else Gst.SeekType.NONE
            stop_ns = int(float(end) * Gst.SECOND) if end is not None else -1
            pb.seek(1.0, Gst.Format.TIME, flags,
                    Gst.SeekType.SET, int(start * Gst.SECOND), stop_type, stop_ns)
        pb.set_property("volume", self._volume / 100.0)
        self._active_bus = pb.get_bus()
        pb.set_state(Gst.State.PLAYING)

    def _play_image(self, path, dur):
        pipe = Gst.Pipeline.new("imgpipe")
        src = Gst.ElementFactory.make("filesrc", None)
        dec = Gst.ElementFactory.make("decodebin", None)
        freeze = Gst.ElementFactory.make("imagefreeze", None)
        conv = Gst.ElementFactory.make("videoconvert", None)
        sink = Gst.ElementFactory.make("kmssink", None)
        if not all([pipe, src, dec, freeze, conv, sink]):
            self.log("image pipeline: missing element")
            return
        src.set_property("location", path)
        for e in (src, dec, freeze, conv, sink):
            pipe.add(e)
        src.link(dec)
        freeze.link(conv)
        conv.link(sink)
        # decodebin exposes its src pad only once the image type is known
        dec.connect("pad-added",
                    lambda _dbin, pad: pad.link(freeze.get_static_pad("sink")))
        self.imgpipe = pipe
        self._active_bus = pipe.get_bus()
        pipe.set_state(Gst.State.PLAYING)
        # images don't EOS (imagefreeze loops); advance via a timer instead
        self._arm_image_timer(dur)

    def _arm_image_timer(self, dur):
        try:
            dur = float(dur) if dur else 0.0
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0:
            return
        gen = self._gen
        t = threading.Timer(dur, self._image_elapsed, args=(gen,))
        t.daemon = True
        self._img_timer = t
        t.start()

    def _image_elapsed(self, gen):
        if gen != self._gen:
            return
        self._emit({"event": "end-file", "reason": "eof"})

    def _cancel_image_timer(self):
        self._gen += 1
        if self._img_timer is not None:
            self._img_timer.cancel()
            self._img_timer = None

    def _stop_video(self):
        if self.playbin is not None:
            self.playbin.set_state(Gst.State.NULL)

    def _stop_image(self):
        if self.imgpipe is not None:
            self.imgpipe.set_state(Gst.State.NULL)
            self.imgpipe = None

    def stop(self):
        """Blank the screen and play nothing (releases the DRM plane)."""
        with self._lock:
            self._cancel_image_timer()
            self._stop_video()
            self._stop_image()
            self._cur_path = None
            self._cur_kind = None
            self._active_bus = None

    # ---- transport / properties ------------------------------------------
    def toggle_pause(self):
        with self._lock:
            if self._cur_kind == "image" or self.playbin is None:
                return  # a frozen image has nothing to pause
            self._paused = not self._paused
            self.playbin.set_state(
                Gst.State.PAUSED if self._paused else Gst.State.PLAYING)

    def get_pause(self):
        return self._paused

    def set_volume(self, vol):
        try:
            vol = max(0, min(100, float(vol)))
        except (TypeError, ValueError):
            return
        self._volume = vol
        if self.playbin is not None:
            self.playbin.set_property("volume", vol / 100.0)

    def get_volume(self):
        return self._volume

    def get_time_pos(self):
        return self._query(Gst.Format.TIME, "position")

    def get_duration(self):
        return self._query(Gst.Format.TIME, "duration")

    def _query(self, fmt, what):
        pb = self.playbin
        if pb is None or self._cur_kind == "image":
            return None
        try:
            if what == "position":
                ok, val = pb.query_position(fmt)
            else:
                ok, val = pb.query_duration(fmt)
        except Exception:
            return None
        if not ok or val < 0:
            return None
        return val / Gst.SECOND

    def set_audio_device(self, name):
        # playbin uses autoaudiosink (system default → HDMI). Per-device
        # selection isn't wired to GStreamer yet; kept as a no-op so callers
        # don't break. TODO: build a device-specific audio sink.
        self.log("audio-device '%s' requested (using system default)" % name)

    def screenshot(self, path):
        """Grab the current frame to `path` by decoding it from the source file
        at the live playback position — kmssink can't be read back directly."""
        with self._lock:
            src = self._cur_path
            kind = self._cur_kind
        if not src or not os.path.exists(src):
            return False
        pos = self.get_time_pos()
        if pos is None:
            pos = self._cur_start
        cmd = ["ffmpeg", "-y", "-nostdin"]
        if kind != "image":
            cmd += ["-ss", "%.3f" % max(0.0, pos)]
        cmd += ["-i", src, "-frames:v", "1", "-q:v", "3",
                "-vf", "scale=640:-2", path]
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            return False
        return os.path.exists(path)

    # ---- bus watching -----------------------------------------------------
    def _watch_bus(self):
        while not self._stop:
            bus = self._active_bus
            if bus is None:
                time.sleep(0.1)
                continue
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.EOS:
                self._emit({"event": "end-file", "reason": "eof"})
            elif msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                self.log("gst error: %s (%s)" % (err, dbg))
                self._emit({"event": "end-file", "reason": "error"})

    def _emit(self, ev):
        for h in list(self.event_handlers):
            try:
                h(ev)
            except Exception as e:  # noqa
                self.log("event handler error: %s" % e)


class PlayerEngine:
    """Drives playback of playlist items through GstPlayer with per-item options."""

    def __init__(self, player, log=print):
        self.player = player
        self.log = log
        self.lock = threading.RLock()
        self.items = []           # resolved item dicts currently playing
        self.index = 0
        self.loop_playlist = False
        self.loops_left = 0       # remaining loops for current item
        self.current = None       # the item dict now playing
        self.playlist_name = None
        self.playing_default = False
        self._gen = 0             # generation counter to cancel stale timers
        player.event_handlers.append(self._on_event)

    # ---- public API -------------------------------------------------------
    def play_playlist(self, playlist):
        with self.lock:
            items = [self._resolve(i) for i in playlist.get("items", [])]
            items = [i for i in items if i]
            if not items:
                self.log("playlist empty: %s" % playlist.get("name"))
                self.play_default()
                return
            self.items = items
            self.index = 0
            self.loop_playlist = bool(playlist.get("loop_playlist", True))
            self.playlist_name = playlist.get("name")
            self.playing_default = False
            self._load_current()

    def reapply(self):
        """Resume playback after a player restart."""
        with self.lock:
            if self.items and not self.playing_default:
                self._load_current()
            else:
                self.play_default()

    def play_default(self):
        with self.lock:
            cfg = config.load()
            item = cfg["settings"].get("default_item")
            self.items = []
            self.playlist_name = None
            self.current = None
            if not item:
                self.playing_default = True
                self._bump_gen()
                self.player.stop()
                return
            ritem = self._resolve(item)
            if not ritem:
                self.playing_default = True
                self.player.stop()
                return
            self.playing_default = True
            self._play_item(ritem)

    def stop(self):
        """Stop the active playlist and fall back to the default item."""
        self.play_default()

    def status(self):
        with self.lock:
            cur = self.current
        return {
            "playing": cur is not None,
            "playing_default": self.playing_default,
            "playlist_name": self.playlist_name,
            "index": self.index,
            "count": len(self.items),
            "current": {
                "file": cur.get("file"),
                "type": cur.get("type"),
            } if cur else None,
            "time_pos": self.player.get_time_pos(),
            "duration": self.player.get_duration(),
            "volume": self.player.get_volume(),
            "paused": bool(self.player.get_pause()),
        }

    # ---- internals --------------------------------------------------------
    def _resolve(self, item):
        try:
            ap = media.abs_path(item["file"])
        except Exception:
            return None
        if not os.path.exists(ap):
            self.log("missing file: %s" % item.get("file"))
            return None
        info = media.probe(item["file"])
        r = dict(item)
        r["_abs"] = ap
        r["_type"] = item.get("type") or media.media_type(item["file"])
        r["_duration"] = info.get("duration")
        r["_has_audio"] = info.get("has_audio")
        r["_codec"] = info.get("codec")
        return r

    def _bump_gen(self):
        self._gen += 1
        return self._gen

    def _load_current(self):
        if not self.items:
            self.play_default()
            return
        if self.index >= len(self.items):
            if self.loop_playlist:
                self.index = 0
            else:
                self.play_default()
                return
        item = self.items[self.index]
        loop = item.get("loop", 1)
        # "always"/0/None => infinite (sentinel -1); otherwise N total plays
        if loop in (0, "always", None):
            self.loops_left = -1
        else:
            self.loops_left = max(1, int(loop))
        self._play_item(item)

    def _play_item(self, item):
        self.current = item
        gen = self._bump_gen()
        t = item["_type"]
        eff_len = None
        if t == "image":
            dur = float(item.get("duration") or 10)
            self.player.load(item["_abs"], kind="image", image_dur=dur)
            eff_len = dur
        else:
            tin = float(item.get("in") or 0)
            tout = item.get("out")
            end = None
            if tout not in (None, "", 0):
                end = float(tout)
                eff_len = end - tin
            elif item.get("_duration"):
                eff_len = float(item["_duration"]) - tin
            self.player.load(item["_abs"], kind=t, start=tin, end=end)
        # volume + fades (videos with audio only)
        vol = int(item.get("volume", 100)) if t != "image" else 100
        fade_in = float(item.get("fade_in") or 0) if t != "image" else 0
        fade_out = float(item.get("fade_out") or 0) if t != "image" else 0
        if fade_in > 0:
            self.player.set_volume(0)
            self._start_fade(gen, 0, vol, fade_in, delay=0)
        else:
            self.player.set_volume(vol)
        if fade_out > 0 and eff_len and eff_len > fade_out:
            self._start_fade(gen, vol, 0, fade_out, delay=eff_len - fade_out)

    def _start_fade(self, gen, frm, to, dur, delay):
        def run():
            if delay:
                time.sleep(delay)
            if gen != self._gen:
                return
            steps = max(1, int(dur * 15))
            for s in range(1, steps + 1):
                if gen != self._gen:
                    return
                v = frm + (to - frm) * (s / steps)
                self.player.set_volume(round(v, 1))
                time.sleep(dur / steps)
        threading.Thread(target=run, daemon=True).start()

    def _on_event(self, ev):
        if ev.get("event") != "end-file":
            return
        if ev.get("reason") not in ("eof", "error"):
            return  # stop/quit handled elsewhere or manual
        with self.lock:
            if self.playing_default:
                if self.current:        # default item loops forever
                    self._play_item(self.current)
                return
            if not self.items:
                return
            if self.loops_left == -1:   # infinite loop on this item
                self._play_item(self.current)
                return
            if self.loops_left > 1:     # more plays of this item remain
                self.loops_left -= 1
                self._play_item(self.current)
                return
            self.index += 1             # advance to next item
            self._load_current()


# module-level singletons, wired in __init__
player = None
engine = None
