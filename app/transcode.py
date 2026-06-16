"""Background transcoding of oversized uploads down to 720p, with progress.

The Raspberry Pi 3 can hardware-*decode* video but has no working zero-copy
display path (see mpv.py), so the GL VO uploads frames and 1080p is too heavy
to play smoothly. 720p plays cleanly, so after upload we detect anything larger
and re-encode it to fit within 1280x720 (H.264/AAC mp4) in a background thread,
exposing progress so the web UI can show a bar.
"""
import os
import subprocess
import threading

from . import media

MAX_W, MAX_H = 1280, 720

# job registry, keyed by the uploaded file's media-relative name
_jobs = {}
_lock = threading.Lock()


def needs_transcode(width, height):
    return bool(width and height and (width > MAX_W or height > MAX_H))


def jobs_snapshot():
    with _lock:
        return {k: dict(v) for k, v in _jobs.items()}


def _set(rel, **kw):
    with _lock:
        j = _jobs.setdefault(rel, {"file": rel})
        j.update(kw)


def _pick_output(rel):
    """Choose a non-colliding 1080p output name (media-relative)."""
    root, _ext = os.path.splitext(rel)
    base = root + ".mp4"
    if base == rel:
        return base  # source is already .mp4 -> replace it in place
    if not os.path.exists(media.abs_path(base)):
        return base
    i = 1
    while True:
        cand = "%s_1080p%s.mp4" % (root, "" if i == 1 else "_%d" % i)
        if not os.path.exists(media.abs_path(cand)):
            return cand
        i += 1


def start(rel, width, height, duration, log=print):
    """Kick off a background transcode of media file `rel` to 1080p."""
    with _lock:
        cur = _jobs.get(rel)
        if cur and cur.get("status") == "running":
            return
        _jobs[rel] = {"file": rel, "status": "running", "percent": 0,
                      "from": "%dx%d" % (width, height), "result": None,
                      "error": None}
    threading.Thread(target=_run, args=(rel, duration, log),
                     daemon=True).start()


def _run(rel, duration, log):
    try:
        src = media.abs_path(rel)
        out_rel = _pick_output(rel)
        out_abs = media.abs_path(out_rel)
    except Exception as e:  # noqa
        _set(rel, status="error", error=str(e))
        return
    tmp_abs = out_abs + ".transcoding.part"
    errf = out_abs + ".transcoding.log"
    # fit inside 1920x1080 preserving aspect; keep dimensions even for yuv420p.
    vf = ("scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease,"
          "scale='trunc(iw/2)*2':'trunc(ih/2)*2'" % (MAX_W, MAX_H))
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src,
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", "-f", "mp4",
           "-progress", "pipe:1", "-nostats", tmp_abs]
    log("transcode start: %s -> %s" % (rel, out_rel))
    try:
        with open(errf, "wb") as ef:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef,
                                    text=True)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") and duration:
                    try:
                        us = int(line.split("=", 1)[1])
                        pct = int(us / 1e6 / duration * 100)
                        _set(rel, percent=max(0, min(99, pct)))
                    except (ValueError, ZeroDivisionError):
                        pass
            proc.wait()
        if proc.returncode != 0:
            tail = _tail(errf)
            _set(rel, status="error",
                 error=tail or ("ffmpeg exited %d" % proc.returncode))
            log("transcode failed: %s: %s" % (rel, tail))
            _rm(tmp_abs)
            return
        os.replace(tmp_abs, out_abs)
        # drop the oversized original if we wrote to a different file
        if os.path.abspath(src) != os.path.abspath(out_abs) and os.path.exists(src):
            _rm(src)
        try:
            media.thumbnail(out_rel)
        except Exception:  # noqa
            pass
        _set(rel, status="done", percent=100, result=out_rel)
        log("transcode done: %s" % out_rel)
    except FileNotFoundError:
        _set(rel, status="error", error="ffmpeg not installed")
        _rm(tmp_abs)
    except Exception as e:  # noqa
        _set(rel, status="error", error=str(e))
        _rm(tmp_abs)
    finally:
        _rm(errf)


def _rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _tail(path, n=400):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()[-n:].strip()
    except OSError:
        return ""
