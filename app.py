"""
Stream Capture server.
  - POST /api/target : set a page URL to capture, OR a direct .m3u8 URL.
  - GET  /api/stream : current stream (raw url + proxy_url).
  - GET  /api/status : state, capture stage, last error.
  - GET  /proxy      : same-origin proxy. Adds Referer/Origin and CORS headers,
                       rewrites HLS playlists so segments also go through here.
  - GET  /           : player page.
"""

import ipaddress
import json
import os
import re
import socket
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

from capture import STAGE, capture_once

BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

STATE = {
    "page_url": CFG["page_url"],
    "manual_url": None,
    "url": None,
    "updated_at": None,
    "last_error": None,
    "cycles": 0,
}
LOCK = threading.Lock()
WAKE = threading.Event()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK_BYTES = 64 * 1024
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024

app = Flask(__name__)


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] [app] {message}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_public_host(hostname):
    """Blocks private, loopback and link-local addresses (SSRF protection)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def is_allowed_target(value):
    parsed = urlparse(value)
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and is_public_host(parsed.hostname)
    )


def is_valid_page_url(value):
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def origin_for(target):
    """Referer origin: the page being captured, or the stream's own host in manual mode."""
    with LOCK:
        page_url = STATE["page_url"] or ""
    parsed = urlparse(page_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    t = urlparse(target)
    return f"{t.scheme}://{t.netloc}"


def read_playlist(resp):
    data = bytearray()
    for chunk in resp.iter_content(CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_PLAYLIST_BYTES:
            raise ValueError("Playlist exceeds size limit")
    return data.decode("utf-8", errors="replace")


def rewrite_playlist(text, base_url):
    """Routes every URI in the playlist (segments, sub-playlists, keys) back through /proxy."""

    def wrap(uri):
        return "/proxy?url=" + quote(urljoin(base_url, uri), safe="")

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
        elif stripped.startswith("#"):
            lines.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{wrap(m.group(1))}"', line))
        else:
            lines.append(wrap(stripped))
    return "\n".join(lines) + "\n"


def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        "Cache-Control": "no-cache",
    }


def capture_loop():
    log("Capture loop started.")
    while True:
        with LOCK:
            manual = STATE["manual_url"]
            page_url = STATE["page_url"]
            STATE["cycles"] += 1
            cycle = STATE["cycles"]

        if manual:
            # Manual .m3u8 mode: no browser capture needed.
            WAKE.wait(CFG["refresh_interval_s"])
            WAKE.clear()
            continue

        log(f"Cycle {cycle} on {page_url}")
        try:
            url = capture_once({**CFG, "page_url": page_url})
            with LOCK:
                # Skip the result if the user changed the target during this cycle.
                if STATE["page_url"] != page_url or STATE["manual_url"]:
                    pass
                elif url:
                    if url != STATE["url"]:
                        STATE["url"] = url
                        STATE["updated_at"] = now_iso()
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
    stream_url = str(body.get("stream_url", "")).strip()

    if stream_url:
        if not is_allowed_target(stream_url):
            return jsonify({"error": "Enter a valid public http(s) .m3u8 URL"}), 400
        with LOCK:
            STATE.update(page_url="", manual_url=stream_url, url=stream_url,
                         updated_at=now_iso(), last_error=None)
        WAKE.set()
        log(f"Manual stream set: {stream_url}")
        return jsonify({"ok": True, "mode": "manual"}), 202

    if not is_valid_page_url(page_url):
        return jsonify({"error": "Enter a full http(s) page URL"}), 400

    with LOCK:
        STATE.update(page_url=page_url, manual_url=None, url=None,
                     updated_at=None, last_error=None)
    WAKE.set()
    log(f"New page target: {page_url}")
    return jsonify({"ok": True, "mode": "capture"}), 202


@app.get("/api/stream")
def api_stream():
    with LOCK:
        url = STATE["url"]
        updated_at = STATE["updated_at"]
        last_error = STATE["last_error"]

    if not url:
        return jsonify({"error": "No stream captured yet", "last_error": last_error}), 404
    return jsonify({
        "url": url,
        "proxy_url": "/proxy?url=" + quote(url, safe=""),
        "updated_at": updated_at,
    })


@app.get("/api/status")
def api_status():
    with LOCK:
        data = dict(STATE)
    data["stage"] = dict(STAGE)
    return jsonify(data)


@app.get("/proxy")
def proxy():
    target = request.args.get("url", "").strip()
    if not is_allowed_target(target):
        return jsonify({"error": "Invalid or disallowed URL"}), 400

    origin = origin_for(target)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": origin + "/",
        "Origin": origin,
    }
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    try:
        upstream = requests.get(target, headers=headers, stream=True,
                                timeout=(10, 30), allow_redirects=True)
    except requests.RequestException as exc:
        return jsonify({"error": f"Upstream failed: {exc}"}), 502

    ctype = upstream.headers.get("Content-Type", "")
    is_playlist = ("mpegurl" in ctype.lower()
                   or urlparse(upstream.url).path.lower().endswith(".m3u8"))

    if is_playlist:
        try:
            body = read_playlist(upstream)
        except ValueError as exc:
            upstream.close()
            return jsonify({"error": str(exc)}), 502
        upstream.close()

        if not body.lstrip().startswith("#EXTM3U"):
            return jsonify({"error": "Upstream did not return an HLS playlist"}), 502

        return Response(
            rewrite_playlist(body, upstream.url),
            status=200,
            content_type="application/vnd.apple.mpegurl",
            headers=cors_headers(),
        )

    # Segments and other binary data are streamed through without buffering.
    def generate():
        try:
            for chunk in upstream.iter_content(CHUNK_BYTES):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    out_headers = cors_headers()
    for name in ("Content-Range", "Accept-Ranges", "Content-Length", "Last-Modified", "ETag"):
        if name in upstream.headers:
            out_headers[name] = upstream.headers[name]

    return Response(
        generate(),
        status=upstream.status_code,
        content_type=ctype or "application/octet-stream",
        headers=out_headers,
    )


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


threading.Thread(target=capture_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
