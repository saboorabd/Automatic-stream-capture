"""
Stream Capture server.
  - Background thread: runs a capture cycle every refresh_interval_s.
  - GET /api/stream : latest captured .m3u8 URL as JSON.
  - GET /           : player page (static/index.html).
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

from capture import capture_once

BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

STATE = {"url": None, "updated_at": None, "last_error": None}
LOCK = threading.Lock()

app = Flask(__name__)


def capture_loop() -> None:
    while True:
        try:
            url = capture_once(CFG)
            with LOCK:
                if url:
                    if url != STATE["url"]:
                        STATE["url"] = url
                        STATE["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    STATE["last_error"] = None
                else:
                    STATE["last_error"] = "No stream request seen within timeout"
        except Exception as exc:
            with LOCK:
                STATE["last_error"] = f"{type(exc).__name__}: {exc}"

        time.sleep(CFG["refresh_interval_s"])


@app.get("/api/stream")
def api_stream():
    with LOCK:
        if not STATE["url"]:
            return jsonify({"error": "No stream captured yet", "last_error": STATE["last_error"]}), 404
        return jsonify({"url": STATE["url"], "updated_at": STATE["updated_at"]})


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


# Start the capture thread once. Run a single worker, or each worker starts its own loop.
threading.Thread(target=capture_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
