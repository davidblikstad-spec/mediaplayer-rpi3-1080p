"""Flask application: auth, web UI, REST API, and wiring of player/scheduler."""
import functools
import os
import time

from flask import (Flask, jsonify, redirect, request, send_file,
                   send_from_directory, session, url_for, render_template, abort)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import cec, config, media, mpv as mpvmod
from .scheduler import Scheduler

_snap_last = {"t": 0.0}


def _public_config(cfg):
    """Config copy safe to send to the browser (no secrets)."""
    import copy
    c = copy.deepcopy(cfg)
    c.get("auth", {}).pop("password_hash", None)
    c.get("settings", {}).pop("secret_key", None)
    return c


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        cfg = config.load()
        if not cfg["auth"].get("password_hash"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "setup required"}), 401
            return redirect(url_for("setup"))
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


def create_app():
    app = Flask(__name__)
    cfg = config.load()
    app.secret_key = cfg["settings"]["secret_key"]
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024 * 1024  # 16 GB uploads

    # ---- player + scheduler singletons -----------------------------------
    log = lambda m: print("[mediaplayer]", m, flush=True)
    mpvmod.mpv = mpvmod.MpvIPC(log=log)
    mpvmod.mpv.start()
    mpvmod.engine = mpvmod.PlayerEngine(mpvmod.mpv, log=log)
    app.scheduler = Scheduler(mpvmod.engine, log=log)
    app.scheduler.reload()
    # start playing the default item on boot
    try:
        mpvmod.engine.play_default()
    except Exception as e:
        log("play_default failed: %s" % e)

    # ================= auth / pages =======================================
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        cfg = config.load()
        if cfg["auth"].get("password_hash"):
            return redirect(url_for("login"))
        if request.method == "POST":
            u = (request.form.get("username") or "").strip()
            p = request.form.get("password") or ""
            if not u or len(p) < 4:
                return render_template("setup.html", error="Username required, password min 4 chars")

            def m(c):
                c["auth"]["username"] = u
                c["auth"]["password_hash"] = generate_password_hash(p)
            config.update(m)
            session["user"] = u
            return redirect(url_for("index"))
        return render_template("setup.html", error=None)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        cfg = config.load()
        if not cfg["auth"].get("password_hash"):
            return redirect(url_for("setup"))
        if request.method == "POST":
            u = (request.form.get("username") or "").strip()
            p = request.form.get("password") or ""
            if u == cfg["auth"]["username"] and check_password_hash(cfg["auth"]["password_hash"], p):
                session["user"] = u
                return redirect(url_for("index"))
            return render_template("login.html", error="Invalid credentials")
        return render_template("login.html", error=None)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template("index.html", username=session.get("user"))

    # ================= state / config =====================================
    @app.route("/api/state")
    @login_required
    def api_state():
        return jsonify({
            "player": mpvmod.engine.status(),
            "next_runs": app.scheduler.next_runs(),
            "mpv_alive": bool(mpvmod.mpv.proc and mpvmod.mpv.proc.poll() is None),
        })

    @app.route("/api/config")
    @login_required
    def api_config():
        return jsonify(_public_config(config.load()))

    # ================= media ==============================================
    @app.route("/api/media")
    @login_required
    def api_media():
        items = media.list_media()
        for it in items:
            it["thumb"] = media.thumbnail(it["file"])
            info = media.probe(it["file"])
            it.update(info)
        return jsonify(items)

    @app.route("/api/upload", methods=["POST"])
    @login_required
    def api_upload():
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "no file"}), 400
        name = secure_filename(f.filename)
        dest = os.path.join(config.MEDIA_DIR, name)
        f.save(dest)
        return jsonify({"ok": True, "file": name, "type": media.media_type(name)})

    @app.route("/api/media/<path:rel>", methods=["DELETE"])
    @login_required
    def api_media_delete(rel):
        try:
            p = media.abs_path(rel)
        except ValueError:
            return jsonify({"error": "bad path"}), 400
        if os.path.exists(p):
            os.remove(p)
        return jsonify({"ok": True})

    @app.route("/media/<path:rel>")
    @login_required
    def serve_media(rel):
        return send_from_directory(config.MEDIA_DIR, rel, conditional=True)

    @app.route("/thumb/<path:name>")
    @login_required
    def serve_thumb(name):
        return send_from_directory(config.THUMB_DIR, name)

    # ================= playlists ==========================================
    @app.route("/api/playlists", methods=["POST"])
    @login_required
    def api_playlist_create():
        body = request.get_json(force=True)

        def m(c):
            pl = {"id": config.new_id(),
                  "name": body.get("name", "Playlist"),
                  "items": [], "loop_playlist": True}
            c["playlists"].append(pl)
            return pl
        return jsonify(config.update(m))

    @app.route("/api/playlists/<pid>", methods=["PUT"])
    @login_required
    def api_playlist_update(pid):
        body = request.get_json(force=True)

        def m(c):
            pl = config.get_playlist(c, pid)
            if not pl:
                return None
            for k in ("name", "items", "loop_playlist"):
                if k in body:
                    pl[k] = body[k]
            return pl
        res = config.update(m)
        if res is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(res)

    @app.route("/api/playlists/<pid>", methods=["DELETE"])
    @login_required
    def api_playlist_delete(pid):
        def m(c):
            c["playlists"] = [p for p in c["playlists"] if p["id"] != pid]
        config.update(m)
        return jsonify({"ok": True})

    # ================= schedules ==========================================
    @app.route("/api/schedules", methods=["POST"])
    @login_required
    def api_schedule_create():
        body = request.get_json(force=True)

        def m(c):
            sch = {"id": config.new_id(), "enabled": True,
                   "name": body.get("name", "Schedule"),
                   "kind": body.get("kind", "play_playlist"),
                   "playlist_id": body.get("playlist_id"),
                   "cec_action": body.get("cec_action", "on"),
                   "time": body.get("time", "08:00"),
                   "days": body.get("days", list(range(7)))}
            c["schedules"].append(sch)
            return sch
        res = config.update(m)
        app.scheduler.reload()
        return jsonify(res)

    @app.route("/api/schedules/<sid>", methods=["PUT"])
    @login_required
    def api_schedule_update(sid):
        body = request.get_json(force=True)

        def m(c):
            for sch in c["schedules"]:
                if sch["id"] == sid:
                    for k in ("enabled", "name", "kind", "playlist_id",
                              "cec_action", "time", "days"):
                        if k in body:
                            sch[k] = body[k]
                    return sch
            return None
        res = config.update(m)
        app.scheduler.reload()
        if res is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(res)

    @app.route("/api/schedules/<sid>", methods=["DELETE"])
    @login_required
    def api_schedule_delete(sid):
        def m(c):
            c["schedules"] = [s for s in c["schedules"] if s["id"] != sid]
        config.update(m)
        app.scheduler.reload()
        return jsonify({"ok": True})

    # ================= settings ===========================================
    @app.route("/api/settings", methods=["PUT"])
    @login_required
    def api_settings():
        body = request.get_json(force=True)

        def m(c):
            for k in ("cec_device", "cec_phys_addr", "default_item",
                      "mpv_extra_args", "video_out", "hw_decode", "audio_out",
                      "screenshot_interval"):
                if k in body:
                    c["settings"][k] = body[k]
        config.update(m)
        # audio-device can be switched live, no player restart needed
        if "audio_out" in body:
            mpvmod.mpv.set_property("audio-device", body["audio_out"] or "auto")
        return jsonify(_public_config(config.load()))

    @app.route("/api/password", methods=["PUT"])
    @login_required
    def api_password():
        body = request.get_json(force=True)
        cfg = config.load()
        if not check_password_hash(cfg["auth"]["password_hash"], body.get("old", "")):
            return jsonify({"error": "wrong current password"}), 403
        new = body.get("new", "")
        if len(new) < 4:
            return jsonify({"error": "password too short"}), 400

        def m(c):
            if body.get("username"):
                c["auth"]["username"] = body["username"].strip()
            c["auth"]["password_hash"] = generate_password_hash(new)
        config.update(m)
        return jsonify({"ok": True})

    # ================= playback control ===================================
    @app.route("/api/play/<pid>", methods=["POST"])
    @login_required
    def api_play(pid):
        cfg = config.load()
        pl = config.get_playlist(cfg, pid)
        if not pl:
            return jsonify({"error": "not found"}), 404
        mpvmod.engine.play_playlist(pl)
        return jsonify({"ok": True})

    @app.route("/api/stop", methods=["POST"])
    @login_required
    def api_stop():
        mpvmod.engine.stop()
        return jsonify({"ok": True})

    @app.route("/api/mpv/restart", methods=["POST"])
    @login_required
    def api_mpv_restart():
        mpvmod.mpv.restart()
        mpvmod.engine.reapply()
        return jsonify({"ok": True})

    @app.route("/api/pause", methods=["POST"])
    @login_required
    def api_pause():
        mpvmod.mpv.command("cycle", "pause")
        return jsonify({"ok": True})

    @app.route("/api/next", methods=["POST"])
    @login_required
    def api_next():
        with mpvmod.engine.lock:
            if mpvmod.engine.items:
                mpvmod.engine.loops_left = 1
                mpvmod.engine.index += 1
                mpvmod.engine._load_current()
        return jsonify({"ok": True})

    # ================= cec ================================================
    @app.route("/api/cec", methods=["POST"])
    @login_required
    def api_cec():
        body = request.get_json(force=True)
        return jsonify(cec.run_action(body.get("action", "on")))

    @app.route("/api/cec/info")
    @login_required
    def api_cec_info():
        return jsonify(cec.info())

    # ================= live HDMI snapshot =================================
    @app.route("/api/snapshot")
    @login_required
    def api_snapshot():
        path = os.path.join(config.PREVIEW_DIR, "live.jpg")
        now = time.time()
        if now - _snap_last["t"] > 1.5:
            _snap_last["t"] = now
            try:
                mpvmod.mpv.command("screenshot-to-file", path, "video", timeout=8)
            except Exception:
                pass
        if not os.path.exists(path):
            abort(404)
        resp = send_file(path, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
