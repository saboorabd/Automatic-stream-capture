"""
Stream Capture server (Enhanced for CORS, Tokens & DRM/HLS Proxy).
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

from capture import STAGE, capture_once, request_reload
from cors import install_cors

BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

M3U8_RE = re.compile(r"\.m3u8(?:$|[?#])", re.IGNORECASE)
CONTROL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]+")

PLAYER_FILES = {"player.js", "player-config.json", "player.css"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK_BYTES = 64 * 1024
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024

LOCK = threading.Lock()
WAKE = threading.Event()

app = Flask(__name__)
install_cors(app)


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] [app] {message}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_url(value):
    return CONTROL_OR_SPACE_RE.sub("", value or "")


def is_public_host(hostname):
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


def is_m3u8(value):
    return bool(M3U8_RE.search(value or ""))


_initial_page = clean_url(CFG.get("page_url", ""))
if is_m3u8(_initial_page):
    STATE = {"page_url": "", "manual_url": _initial_page, "url": _initial_page,
             "updated_at": now_iso(), "last_error": None, "cycles": 0}
else:
    STATE = {"page_url": _initial_page, "manual_url": None, "url": None,
             "updated_at": None, "last_error": None, "cycles": 0}


def origin_for(target):
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
    """Routes every URI in the playlist (segments, sub-playlists, decryption keys) back through /proxy."""

    def wrap(uri):
        absolute_url = urljoin(base_url, uri)
        return "/proxy?url=" + quote(absolute_url, safe="")

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
        elif stripped.startswith("#"):
            # Handles #EXT-X-KEY, #EXT-X-MAP, #EXT-X-STREAM-INF URIs correctly
            lines.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{wrap(m.group(1))}"', line))
        else:
            lines.append(wrap(stripped))
    return "\n".join(lines) + "\n"


def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type, Authorization, X-Requested-With",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, X-Content-Duration",
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

        if manual or not page_url:
            WAKE.wait(CFG["refresh_interval_s"])
            WAKE.clear()
            continue

        log(f"Cycle {cycle} on {page_url}")
        try:
            url = capture_once({**CFG, "page_url": page_url})
            with LOCK:
                if STATE["page_url"] != page_url or STATE["manual_url"]:
                    pass
                elif url:
                    if url != STATE["url"]:
                        STATE["url"] = url
                        STATE["updated_at"] = now_iso()
                    STATE["last_error"] = None
                else:
                    STATE["last_error"] = STAGE.get("error") or "No stream found within timeout"
        except Exception as exc:
            log(f"Cycle {cycle} failed:\n{traceback.format_exc()}")
            with LOCK:
                STATE["last_error"] = f"{type(exc).__name__}: {exc}"

        WAKE.wait(CFG["refresh_interval_s"])
        WAKE.clear()


@app.post("/api/target")
def api_target():
    body = request.get_json(silent=True) or {}
    value = clean_url(str(body.get("page_url") or body.get("stream_url") or ""))
    if not value:
        return jsonify({"error": "Paste a page URL or a .m3u8 link"}), 400

    if is_m3u8(value):
        if not is_allowed_target(value):
            return jsonify({"error": "Enter a valid public http(s) .m3u8 URL"}), 400
        with LOCK:
            STATE.update(page_url="", manual_url=value, url=value,
                         updated_at=now_iso(), last_error=None)
        WAKE.set()
        log(f"Manual stream set: {value}")
        return jsonify({"ok": True, "mode": "manual"}), 202

    if not is_valid_page_url(value):
        return jsonify({"error": "Enter a full http(s) page URL or .m3u8 link"}), 400

    with LOCK:
        STATE.update(page_url=value, manual_url=None, url=None,
                     updated_at=None, last_error=None)
    WAKE.set()
    log(f"New page target: {value}")
    return jsonify({"ok": True, "mode": "capture"}), 202


@app.post("/api/refresh")
def api_refresh():
    request_reload()
    WAKE.set()
    log("Refresh requested by viewer.")
    return jsonify({"ok": True}), 202


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


@app.route("/proxy", methods=["GET", "OPTIONS"])
def proxy():
    # Handle preflight options request for CORS
    if request.method == "OPTIONS":
        return Response(status=204, headers=cors_headers())

    target = clean_url(request.args.get("url", ""))
    if not is_allowed_target(target):
        log(f"proxy rejected url: {target[:120]!r}")
        return jsonify({"error": "Invalid or disallowed URL"}), 400

    origin = origin_for(target)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": origin + "/",
        "Origin": origin,
    }
    
    # Forward client Range header for video chunk streaming if present
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    try:
        upstream = requests.get(target, headers=headers, stream=True,
                                timeout=(10, 30), allow_redirects=True)
    except requests.RequestException as exc:
        log(f"proxy upstream error for {urlparse(target).hostname}: {type(exc).__name__}")
        return jsonify({"error": f"Upstream failed: {exc}"}), 502

    if upstream.status_code >= 400:
        log(f"proxy upstream {upstream.status_code} for {urlparse(target).hostname}")
        upstream.close()
        return jsonify({"error": f"Upstream responded {upstream.status_code}"}), 502

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
            log(f"proxy not a playlist from {urlparse(target).hostname}: {body[:80]!r}")
            return jsonify({"error": "Upstream did not return an HLS playlist"}), 502

        return Response(
            rewrite_playlist(body, upstream.url),
            status=200,
            content_type="application/vnd.apple.mpegurl",
            headers=cors_headers(),
        )

    def generate():
        try:
            for chunk in upstream.iter_content(CHUNK_BYTES):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    out_headers = cors_headers()
    for name in ("Content-Range", "Accept-Ranges", "Content-Length", "Last-Modified", "ETag", "Content-Type"):
        if name in upstream.headers:
            out_headers[name] = upstream.headers[name]

    return Response(
        generate(),
        status=upstream.status_code,
        content_type=ctype or "application/octet-stream",
        headers=out_headers,
    )


@app.get("/<name>")
def player_asset(name):
    if name not in PLAYER_FILES:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(BASE_DIR / "static", name)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


threading.Thread(target=capture_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
