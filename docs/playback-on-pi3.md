# Video playback on the Raspberry Pi 3 — findings & tuning

This documents why playback behaves the way it does on this machine (Pi 3,
Raspberry Pi OS / Debian 13, **console — no desktop**, mpv on DRM/KMS), what was
changed, and what the options are for getting smooth 1080p later.

## TL;DR

- The Pi 3 can **hardware-decode** H.264, but on a bare KMS console it cannot
  get the decoded frames to the screen **zero-copy** through mpv. The only path
  that actually displays requires a per-frame CPU YUV→RGB conversion, which is
  too heavy for 1080p (plays at ~0.25×).
- **4K** can't be hardware-decoded at all on a Pi 3.
- **Current solution:** auto-transcode uploads down to **720p** and play via
  `--vo=gpu --hwdec=v4l2m2m-copy`. 720p plays smoothly; a TV upscales it fine.

## What we observed (diagnosed live via the mpv IPC socket)

1. **4K `.mov` upload failed** — large uploads were spooled to `/tmp`, which is a
   small tmpfs (RAM disk). Fixed by pointing `TMPDIR` at the SD card (`run.py`).
2. **4K can't decode** on a Pi 3 → added background auto-transcode (`transcode.py`).
3. **1080p was choppy.** The decoder kept up (`decoder-frame-drop-count = 0`) but
   playback ran at 0.24–0.74×. Root-cause chain:
   - The original code forced the decoder per file with `--vd=h264_v4l2m2m`. That
     **silently falls back to software decoding** whenever a real display VO is
     active (≈9 fps for 1080p → the lag).
   - Switched to `--hwdec=v4l2m2m-copy`: hardware decode engaged, but `vo=drm`/
     `vo=gpu` then **CPU-converts YUV→RGB every frame** (~230% CPU, 0.24×).
   - Every zero-copy display path failed on this stack:
     - `vo=drm` + non-copy + overlay plane → mpv **downloads** the frames and
       software-scales anyway.
     - `vo=gpu` + non-copy → `drmprime hwdec requires at least one dmabuf interop
       backend → Loading failed` (the V3D **GLES 2.0** context is too old for
       mpv's GL dmabuf interop); falls back to a DRM overlay whose atomic commit
       fails on vc4 (`Error 22 / EINVAL`) → black screen / first-frame-only.
     - Forcing GLES (`--opengl-es=yes`) → same interop failure.

## Root cause

On a bare KMS console **mpv owns the display** and must composite the planes
itself. On this Pi 3 + mesa stack, mpv's GL dmabuf interop won't load and its DRM
overlay atomic commit is rejected by vc4. So hardware-decoded frames can only
reach the screen via a CPU conversion — fine for 720p, too slow for 1080p.

## Current solution (in this repo)

- `app/transcode.py` targets **1280×720** (uploads larger than 720p auto-transcode).
- `app/mpv.py` uses `--vo=gpu --hwdec=v4l2m2m-copy` — the reliable displaying path.
- **Known limitation:** transcoding *on the Pi* is slow (the libx264 encode runs
  at ~0.1× realtime), so converting a long clip takes many minutes. Could be sped
  up using the Pi's hardware encoder (`h264_v4l2m2m`) — **TODO**.

## Options for smooth 1080p later

1. **Pi 4 / Pi 5** — proper video pipeline; plays 1080p (Pi 4 also 4K). Cleanest;
   the app runs unchanged. *Recommended if 1080p is a hard requirement.*
2. **Wayland desktop on the Pi 3** — a compositor owns the display and can scan
   the decoded dmabuf onto a hardware plane (zero-copy), sidestepping both
   failures above. Likely smooth for 1080p25/30; **not verified**, and the Pi 3
   is still weak (more RAM/CPU baseline used by the desktop).
3. **Stay at 720p** (current) — works today, no further effort.

## Would Wayland mean rewriting everything? No.

It's a change to the **display/launch layer only**:

**Unchanged:** Flask app, REST API, web UI, playlists, schedules, media library,
transcode, HDMI-CEC, the mpv IPC engine (`PlayerEngine`), live HDMI snapshot.

**Changes:**
- *Boot/deploy* (`mediaplayer.service`, `install.sh`): instead of the service
  taking over tty1 to grab DRM master, run a kiosk Wayland compositor (e.g.
  `cage` or `labwc`) and launch/keep mpv as a client inside it.
- *mpv launch* (`app/mpv.py` `_mpv_args` / `ensure_running`): run mpv under the
  compositor (set `WAYLAND_DISPLAY`), drop `--gpu-context=drm`, use `--vo=gpu`
  or `--vo=dmabuf-wayland`, and keep non-copy `--hwdec=v4l2m2m` so frames pass to
  the compositor as dmabuf.

Estimate: a focused change to mpv's launch plus the service unit / installer.
The core application is untouched.
