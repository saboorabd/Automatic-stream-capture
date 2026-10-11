"""
Stream Capture server.
  - POST /api/target : set the page URL to capture (from the web form).
  - GET  /api/stream : latest captured .m3u8 URL as JSON.
  - GET  /api/status : current state, stage, and last error.
  - GET  /           : player page with the URL form.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import traceback
import time

from flask import Flask, jsonify, request, send_from_directory

from capture import STAGE, capture_once

BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

STATE = {
    "page_url": CFG["page_url"],
    "url": None,
    "updated_at": None,
    "last_error": None,
    "cycles": 0,
}
LOCK = threading.Lock()
WAKE = threading.Event()  # Set when a new page URL arrives, so capture starts now.

app = Flask(__name__)


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] [app] {message}", flush=True)


def is_valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def capture_loop() -> None:
    log("Capture loop started.")
    while True:
        with LOCK:
            STATE["cycles"] += 1
            cycle = STATE["cycles"]
            page_url = STATE["page_url"]
        log(f"Cycle {cycle} on {page_url}")

        try:
            run_cfg = {**CFG, "page_url": page_url}
            url = capture_once(run_cfg)
            with LOCK:
                if url:
                    if url != STATE["url"]:
                        STATE["url"] = url
                        STATE["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    STATE["last_error"] = None
                else:
                    STATE["last_error"] = "No stream request seen within timeout"
        except Exception as exc:
            log(f"Cycle {cycle} failed:\n{traceback.format_exc()}")
            with LOCK:
                STATE["last_error"] = f"{type(exc).__name__}: {exc}"

        WAKE.wait(CFG["refresh_interval_s"])
        WAKE.clear()


@app.post("/api/target")
def api_target():
    body = request.get_json(silent=True) or {}
    page_url = str(body.get("page_url", "")).strip()

    if not is_valid_http_url(page_url):
        return jsonify({"error": "Enter a full http(s) URL"}), 400

    with LOCK:
        STATE["page_url"] = page_url
        STATE["url"] = None
        STATE["updated_at"] = None
        STATE["last_error"] = None

    WAKE.set()
    log(f"New target set: {page_url}")
    return jsonify({"ok": True, "page_url": page_url}), 202


@app.get("/api/stream")
def api_stream():
    with LOCK:
        if not STATE["url"]:
