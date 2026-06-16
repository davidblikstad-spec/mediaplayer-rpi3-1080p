"""mpv process management, JSON-IPC client, and the playlist playback engine."""
import json
import os
import shlex
import socket
import subprocess
import threading
import time

from . import config, media

SOCKET_PATH = "/tmp/mediaplayer-mpv.sock"

# Hardware decode on the Pi uses the V4L2 mem2mem codec block (/dev/video10),
# engaged via mpv's --hwdec (set at startup; mpv auto-selects the right decoder
# per codec). The mode must match the video output:
#   vo=drm : "v4l2m2m" (non-copy) — decoded frames stay as DRM-prime buffers and
#            are shown directly on a hardware plane (zero-copy, no CPU work).
#   vo=gpu : "v4l2m2m-copy" — frames are pulled into system memory for the GL VO.
# Using the copy mode with vo=drm forces a CPU YUV->RGB conversion per frame,
# which pins ~2.5 cores on a Pi 3 and plays 1080p at ~0.25x. (Forcing the
# decoder per-file via --vd= instead silently falls back to software entirely.)
def _hwdec_for(vo):
    return "v4l2m2m" if vo == "drm" else "v4l2m2m-copy"


class MpvIPC:
    """Manages an mpv process rendering to DRM and talks to it over JSON IPC."""

    def __init__(self, log=print):
        self.log = log
        self.proc = None
        self.sock = None
        self._sock_lock = threading.Lock()
        self._req_id = 0
        self._pending = {}
        self._pending_lock = threading.Lock()
        self.event_handlers = []          # list of callables(event_dict)
        self._reader = None
        self._stop = False

    # ---- process lifecycle ------------------------------------------------
    def _mpv_args(self):
        cfg = config.load()
        vo = cfg["settings"].get("video_out", "gpu")
        if vo == "drm":
            vo_args = ["--vo=drm"]
        else:
            # GPU output (V3D) scales on the GPU instead of the CPU — essential
            # on a Pi when the panel resolution differs from the video size.
            vo_args = ["--vo=gpu", "--gpu-context=drm"]
        hwdec = _hwdec_for(vo) if cfg["settings"].get("hw_decode", True) else "no"
        args = [
            "mpv",
            "--idle=yes",
            "--force-window=no",
            *vo_args,
            "--hwdec=" + hwdec,
            "--keep-open=no",
            "--no-config",
            "--no-osc",
            "--no-osd-bar",
            "--no-input-default-bindings",
            "--no-terminal",
            "--really-quiet",
            "--audio-fallback-to-null=yes",
            "--image-display-duration=inf",
            "--audio-device=" + (cfg["settings"].get("audio_out") or "auto"),
            "--loop-file=no",
            "--input-ipc-server=" + SOCKET_PATH,
        ]
        extra = cfg["settings"].get("mpv_extra_args", "").strip()
        if extra:
            args += shlex.split(extra)
        return args

    def start(self):
        self._stop = False
        self.ensure_running()
        t = threading.Thread(target=self._supervise, daemon=True)
        t.start()

    def restart(self):
        """Kill and relaunch mpv with fresh config (e.g. after changing vo)."""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self._teardown_socket()
        self.proc = None
        self.ensure_running()

    def _supervise(self):
        while not self._stop:
            time.sleep(2)
            if self.proc and self.proc.poll() is not None:
                self.log("mpv exited; restarting")
                self._teardown_socket()
                self.ensure_running()

    def ensure_running(self):
        if self.proc and self.proc.poll() is None and self.sock:
            return
        if os.path.exists(SOCKET_PATH):
            try:
                os.remove(SOCKET_PATH)
            except OSError:
                pass
        try:
            self.proc = subprocess.Popen(
                self._mpv_args(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.log("mpv not installed")
            return
        self._connect()

    def _connect(self):
        for _ in range(50):
            if os.path.exists(SOCKET_PATH):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(SOCKET_PATH)
                    self.sock = s
                    self._reader = threading.Thread(target=self._read_loop, daemon=True)
                    self._reader.start()
                    self.log("connected to mpv")
                    return
                except OSError:
                    pass
            time.sleep(0.2)
        self.log("could not connect to mpv socket")

    def _teardown_socket(self):
        with self._sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock = None

    # ---- IPC --------------------------------------------------------------
    def _read_loop(self):
        buf = b""
        sock = self.sock
        while not self._stop and sock:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if "request_id" in msg:
                    with self._pending_lock:
                        ev = self._pending.get(msg["request_id"])
                        if ev:
                            ev["result"] = msg
                            ev["event"].set()
                elif "event" in msg:
                    for h in list(self.event_handlers):
                        try:
                            h(msg)
                        except Exception as e:  # noqa
                            self.log("event handler error: %s" % e)
        self._teardown_socket()

    def command(self, *args, timeout=5):
        with self._pending_lock:
            self._req_id += 1
            rid = self._req_id
            slot = {"event": threading.Event(), "result": None}
            self._pending[rid] = slot
        payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
        with self._sock_lock:
            if not self.sock:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                return None
            try:
                self.sock.sendall(payload.encode("utf-8"))
            except OSError:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                return None
        ok = slot["event"].wait(timeout)
        with self._pending_lock:
            self._pending.pop(rid, None)
        if not ok:
            return None
        return slot["result"]

    def get_property(self, name):
        r = self.command("get_property", name)
        if r and r.get("error") == "success":
            return r.get("data")
        return None

    def set_property(self, name, value):
        return self.command("set_property", name, value)


class PlayerEngine:
    """Drives playback of playlist items through MpvIPC with per-item options."""

    def __init__(self, mpv, log=print):
        self.mpv = mpv
        self.log = log
        self.lock = threading.RLock()
        self.items = []           # resolved item dicts currently playing
        self.index = 0
        self.loop_playlist = False
        self.loops_left = 0       # remaining loops for current item
        self.current = None       # the item dict now playing
        self.playlist_name = None
        self.playing_default = False
        self._fade = None         # active fade thread token
        self._gen = 0             # generation counter to cancel stale timers
        mpv.event_handlers.append(self._on_event)

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
        """Resume playback after an mpv restart."""
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
                self.mpv.command("stop")
                return
            ritem = self._resolve(item)
            if not ritem:
                self.playing_default = True
                self.mpv.command("stop")
                return
            self.playing_default = True
            self._play_item(ritem)

    def stop(self):
        """Stop the active playlist and fall back to the default item."""
        self.play_default()

    def status(self):
        with self.lock:
            cur = self.current
        paused = self.mpv.get_property("pause")
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
            "time_pos": self.mpv.get_property("time-pos"),
            "duration": self.mpv.get_property("duration"),
            "volume": self.mpv.get_property("volume"),
            "paused": bool(paused),
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
        opts = []
        eff_len = None
        if t == "image":
            dur = float(item.get("duration") or 10)
            opts.append("image-display-duration=%g" % dur)
            eff_len = dur
        else:
            tin = float(item.get("in") or 0)
            tout = item.get("out")
            if tin > 0:
                opts.append("start=%g" % tin)
            if tout not in (None, "", 0):
                opts.append("end=%g" % float(tout))
                eff_len = float(tout) - tin
            elif item.get("_duration"):
                eff_len = float(item["_duration"]) - tin
            # hardware decode is handled globally via --hwdec (see _mpv_args)
        optstr = ",".join(opts)
        # loadfile <url> replace <index=0> <options>
        self.mpv.command("loadfile", item["_abs"], "replace", 0, optstr)
        # volume + fades (videos with audio only)
        vol = int(item.get("volume", 100)) if t != "image" else 100
        fade_in = float(item.get("fade_in") or 0) if t != "image" else 0
        fade_out = float(item.get("fade_out") or 0) if t != "image" else 0
        if fade_in > 0:
            self.mpv.set_property("volume", 0)
            self._start_fade(gen, 0, vol, fade_in, delay=0)
        else:
            self.mpv.set_property("volume", vol)
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
                self.mpv.set_property("volume", round(v, 1))
                time.sleep(dur / steps)
        threading.Thread(target=run, daemon=True).start()

    def _on_event(self, ev):
        if ev.get("event") != "end-file":
            return
        if ev.get("reason") != "eof":
            return  # stop/quit/error handled elsewhere or manual
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
mpv = None
engine = None
