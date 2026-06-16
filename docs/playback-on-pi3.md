# Video playback on the Raspberry Pi 3 — findings & tuning

This documents how playback works on this machine (Pi 3, Raspberry Pi OS /
Debian 13, **console — no desktop**, GStreamer on DRM/KMS) and how we got to
smooth native 1080p.

## TL;DR

- The Pi 3 hardware-decodes H.264 **and** can scan the decoded frames out to a
  **DRM hardware plane zero-copy** — giving smooth **native 1080p25** with the
  CPU near idle. The player uses GStreamer:
  `… ! v4l2h264dec ! kmssink` (auto-plugged via `playbin3`).
- **4K** still can't be hardware-decoded on a Pi 3, so uploads larger than
  1080p auto-transcode down to 1080p (`app/transcode.py`).
- We **switched the backend from mpv to GStreamer** (`app/gst.py`). The earlier
  720p ceiling and the "needs a Pi 4 or a Wayland desktop" options are gone.

## Why we moved off mpv

The original backend was mpv on a bare KMS console, and 1080p was choppy. The
decoder kept up, but **mpv** couldn't get the frames to screen zero-copy:

- `--vd=h264_v4l2m2m` silently fell back to **software** decode behind a real VO
  (~9 fps).
- `--hwdec=v4l2m2m-copy` engaged HW decode, but `vo=drm`/`vo=gpu` then
  **CPU-converted YUV→RGB every frame** (~230% CPU, 0.24×).
- Every mpv zero-copy display path failed on this stack: its GL dmabuf interop
  won't load (the V3D GLES 2.0 context is too old), and its DRM overlay plane
  atomic commit is rejected by vc4 (`Error 22 / EINVAL`).

The conclusion *at the time* was "no zero-copy path exists on this stack." That
was an **mpv** limitation, not a hardware one. `scripts/gst-test.sh` proved it:
GStreamer's `kmssink` performs exactly the vc4 atomic commit that mpv's overlay
couldn't, putting the decoder's NV12 dmabuf straight onto a hardware plane —
smooth 1080p, no CPU conversion.

## Current solution (in this repo)

- **`app/gst.py`** — `GstPlayer` drives playback via PyGObject:
  - Video/audio: `playbin3` with `video-sink=kmssink`. `v4l2h264dec` is
    auto-plugged (rank primary+1). playbin gives native volume, seek (used for
    in/out trim), position/duration and EOS.
  - Still images: `filesrc ! decodebin ! imagefreeze ! videoconvert ! kmssink`,
    held for the item's display duration.
  - Only one pipeline holds the DRM plane at a time; the other is set to NULL
    (releasing the plane) before the active one starts.
  - EOS on the bus is surfaced as `{"event":"end-file","reason":"eof"}`, the same
    contract `PlayerEngine` consumed from mpv — so the playlist/loop/fade engine
    was reused almost verbatim.
- **`app/transcode.py`** targets **1920×1080** (only >1080p uploads transcode).
- Dependencies (`python3-gi`, the GStreamer GIR typelibs, the gst plugin stack)
  are installed by `install.sh`, which also flips the venv's
  `include-system-site-packages` on so `gi` is importable.

## Testing

`sudo scripts/gst-test.sh [FILE]` — the raw pipeline smoke test (no app code).

`sudo scripts/gst-player-test.sh [VIDEO] [IMAGE]` — exercises the real
`GstPlayer` class: plays, checks position advances, pause/resume, screenshot,
image display, and dumps the plugged elements to confirm the path stayed
zero-copy (hardware decoder present, no `videoconvert`). Both stop the
mediaplayer service to free DRM master and restore it on exit.

## Known limitations / follow-ups

- **Audio device selection**: playbin uses `autoaudiosink` (system default →
  HDMI). The `audio_out` setting is not yet wired to a specific GStreamer sink
  (`GstPlayer.set_audio_device` is a no-op). TODO.
- The legacy `video_out` / `hw_decode` / `mpv_extra_args` settings are now inert
  (kept so the existing settings UI doesn't break). Prune from the UI later.
- **Live HDMI snapshot** is captured by decoding one frame from the source file
  at the current position (`ffmpeg`), since `kmssink` can't be read back. It
  reflects the file, which is what's on screen.
- Transcoding *on the Pi* is slow (libx264 ~0.1× realtime). Could use the Pi's
  hardware encoder (`v4l2h264enc`/`h264_v4l2m2m`) — TODO.
