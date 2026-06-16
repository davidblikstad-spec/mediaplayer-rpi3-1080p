"""Persistent JSON configuration store with a write lock."""
import json
import os
import threading
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
THUMB_DIR = os.path.join(BASE_DIR, "thumbs")
PREVIEW_DIR = os.path.join(BASE_DIR, "previews")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

for d in (DATA_DIR, MEDIA_DIR, THUMB_DIR, PREVIEW_DIR):
    os.makedirs(d, exist_ok=True)

_lock = threading.RLock()


def new_id():
    return uuid.uuid4().hex[:12]


def _defaults():
    return {
        "auth": {"username": None, "password_hash": None},
        "settings": {
            "secret_key": uuid.uuid4().hex,
            "host": "0.0.0.0",
            "port": 8080,
            "cec_device": "/dev/cec0",
            "cec_phys_addr": "",          # e.g. 0x1000; blank = auto/skip
            "default_item": None,          # an item dict (see playlists) or None
            "mpv_extra_args": "",
            "video_out": "gpu",            # "gpu" (GPU-scaled, recommended) or "drm"
            "audio_out": "auto",           # mpv --audio-device; "auto", HDMI or analog
            "hw_decode": True,             # use Pi V4L2 hardware video decoder
            "screenshot_interval": 5,      # seconds between HDMI snapshots
        },
        "playlists": [],
        "schedules": [],
    }


def load():
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            cfg = _defaults()
            _write(cfg)
            return cfg
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        # merge in any new default keys
        d = _defaults()
        for k, v in d.items():
            cfg.setdefault(k, v)
        for k, v in d["settings"].items():
            cfg["settings"].setdefault(k, v)
        return cfg


def _write(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def save(cfg):
    with _lock:
        _write(cfg)


def update(mutator):
    """Load, apply mutator(cfg), save, and return the result of the mutator."""
    with _lock:
        cfg = load()
        result = mutator(cfg)
        _write(cfg)
        return result


def get_playlist(cfg, pid):
    for p in cfg["playlists"]:
        if p["id"] == pid:
            return p
    return None
