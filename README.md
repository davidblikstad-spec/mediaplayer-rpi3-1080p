# Media Player — HDMI digital signage for Raspberry Pi

A self-hosted video/image player for the Pi's HDMI output, configured from a
web interface. Built for this machine: Raspberry Pi 3 / Debian 13, console
(no desktop), playing directly to DRM/KMS via **GStreamer** (hardware H.264
decode → DRM hardware plane via kmssink — smooth native 1080p).

## Features

- **Web UI with login** (username + password, stored hashed; session cookie).
- **Fullscreen HDMI playback** of videos and images via GStreamer on DRM/KMS
  (hardware-decoded, zero-copy — native 1080p on a Pi 3).
- **Playlists** of video + image items, with per-item:
  - **In / out trim** (videos) and **display duration** (images),
  - **Loop count** — a fixed number of times or **always**,
  - **Volume** (0–130) with **fade-in** and **fade-out** (videos),
- **Loop the whole playlist** on/off.
- **Default content** — an item looped forever whenever nothing else is playing.
- **Scheduling** (day-of-week + time) of:
  - playing a playlist,
  - stopping (back to default),
  - **HDMI-CEC** display **On / Off / Set-as-source**.
- **Manual HDMI-CEC** buttons + adapter detection.
- **Preview files** in the browser, and a **periodic snapshot of the live HDMI
  output**.

## Install (autostart on boot)

```bash
cd /home/david/mediaplayer
sudo ./install.sh
```

This installs the GStreamer/PyGObject dependencies, then a systemd service that
takes over **tty1** (the HDMI console) so the player can drive the display, and
starts it on boot. Then browse to `http://<pi-ip>:8080` and create your admin
account on first visit.

> The service runs a login session on tty1 (`PAMName=login`) so it becomes the
> active seat session — required to get DRM master and output to HDMI.
> `getty@tty1` is disabled by the installer so the two don't fight over tty1.

## Run manually (for testing)

```bash
cd /home/david/mediaplayer
./venv/bin/python run.py
```
Note: run from the **active console**, not over SSH — the player needs DRM
master to show video, so video output and the live snapshot only work on the
console (or via the systemd service).

## Usage notes

- **Upload media** in the *Media Library* tab (or drop files into `media/`).
- Build a **playlist**, add files, set in/out, loops, volume, fades, **Save**,
  then **▶ Play now** or schedule it.
- **Default content** and **CEC** are configured in *Settings*. Use *Detect* to
  read the HDMI physical address for CEC "set source".
- **In-browser preview** plays the original file; the browser must support the
  codec (mp4/H.264 and webm preview best; mkv/avi may not preview in-browser
  but still play fine on HDMI via GStreamer).

## Layout

```
app/            Flask app: config, media, gst player engine, cec, scheduler, routes
app/templates/  login / setup / index pages
app/static/     app.js + style.css
data/config.json   all configuration (created on first run)
media/          uploaded video/image files
thumbs/         generated thumbnails
previews/       live HDMI snapshot
venv/           Python environment (flask, apscheduler, waitress)
```

## Service control

```bash
journalctl -u mediaplayer -f        # live logs
sudo systemctl restart mediaplayer
sudo systemctl stop mediaplayer
```

## Security

Login is username + password (hashed with Werkzeug PBKDF2). Traffic is plain
HTTP — fine on a trusted LAN. For exposure beyond the LAN, put it behind a
reverse proxy (e.g. Caddy/nginx) with TLS.
```
