#!/usr/bin/env python3
"""Entry point: serve the media-player web app with a production WSGI server."""
from app import create_app
from app import config

app = create_app()

if __name__ == "__main__":
    cfg = config.load()
    host = cfg["settings"].get("host", "0.0.0.0")
    port = int(cfg["settings"].get("port", 8080))
    try:
        from waitress import serve
        print("[mediaplayer] serving on http://%s:%d (waitress)" % (host, port), flush=True)
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print("[mediaplayer] serving on http://%s:%d (flask dev server)" % (host, port), flush=True)
        app.run(host=host, port=port, threaded=True)
