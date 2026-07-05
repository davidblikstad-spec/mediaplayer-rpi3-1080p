"""Background transcoding of oversized uploads down to 1080p, with progress.

The Pi 3 plays 1080p H.264 smoothly through the GStreamer backend (hardware
decode → kmssink DRM plane, see app/gst.py), but it cannot hardware-decode 4K.
So after upload we detect anything larger than 1080p and re-encode it to fit
within 1920x1080 (H.264/AAC mp4) in a background thread, exposing progress so
the web UI can show a bar.
"""
import os
import subprocess
import threading

from . import media

MAX_W, MAX_H = 1920, 1080
# Codecs the Pi can hardware-decode smoothly; anything else gets transcoded.
PLAYABLE_VCODECS = {"h264"}

# Images are capped by width only (kmssink/display is 1080p; a wider-than-1080p
# still just wastes decode memory and bandwidth). Height follows the aspect
# ratio. Unlike video this is a quick one-shot resize, so it runs synchronously.
MAX_IMAGE_W = 1920


def needs_image_downscale(width):
    """True if an image is wider than MAX_IMAGE_W. Images at or below that width
    are left untouched (we only ever downscale, never upscale)."""
    return bool(width and width > MAX_IMAGE_W)


def downscale_image(rel, log=print):
    """Scale an oversized image down to MAX_IMAGE_W wide, preserving aspect
    ratio, in place. Returns True if the file was rewritten.

    The video sibling (`start`/`_run`) is a backgrounded, progress-tracked
    re-encode; an image resize is sub-second, so this is synchronous. It mirrors
    the fit-and-replace shape: resize to a temp file, atomically swap it in, and
    refresh the thumbnail. `scale='min(W,iw)'` guarantees we never upscale."""
    try:
        src = media.abs_path(rel)
    except Exception as e:  # noqa
        log("image downscale: bad path %s: %s" % (rel, e))
        return False
    if media.media_type(rel) != "image" or not os.path.exists(src):
        return False
    root, ext = os.path.splitext(src)
    tmp_abs = "%s.resizing.part%s" % (root, ext)   # keep ext so ffmpeg picks the format
    vf = "scale='min(%d,iw)':-1" % MAX_IMAGE_W     # cap width, height auto (keep ratio)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src, "-vf", vf, tmp_abs]
    log("image downscale start: %s" % rel)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(tmp_abs):
            log("image downscale failed: %s: %s"
                % (rel, (r.stderr or b"")[-300:].decode("utf-8", "replace")))
            _rm(tmp_abs)
            return False
        os.replace(tmp_abs, src)
        try:
            media.thumbnail(rel)
        except Exception:  # noqa
            pass
        log("image downscale done: %s" % rel)
        return True
    except FileNotFoundError:
        log("image downscale: ffmpeg not installed")
        _rm(tmp_abs)
        return False
    except Exception as e:  # noqa
        log("image downscale error: %s: %s" % (rel, e))
        _rm(tmp_abs)
        return False

# job registry, keyed by the uploaded file's media-relative name
_jobs = {}
_lock = threading.Lock()


def needs_transcode(width, height, codec=None):
    """A video needs transcoding if it's larger than 1080p OR not in a codec
    the Pi can hardware-decode (H.264) — independent reasons."""
    oversize = bool(width and height and (width > MAX_W or height > MAX_H))
    badcodec = bool(codec) and codec not in PLAYABLE_VCODECS
    return oversize or badcodec


def jobs_snapshot():
    # private keys (the Popen handle, abort flag) start with "_" and are dropped
    with _lock:
        return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                for k, v in _jobs.items()}


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
            _set(rel, _proc=proc)
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
        with _lock:
            aborted = bool(_jobs.get(rel, {}).get("_aborted"))
        if aborted:
            _set(rel, status="aborted", percent=0, error=None)
            log("transcode aborted: %s" % rel)
            _rm(tmp_abs)
            return
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


def cancel(rel):
    """Abort a running transcode for `rel`. Returns True if one was running."""
    with _lock:
        j = _jobs.get(rel)
        if not j or j.get("status") != "running":
            return False
        j["_aborted"] = True
        p = j.get("_proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except Exception:  # noqa
            try:
                p.kill()
            except Exception:  # noqa
                pass
    return True


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
