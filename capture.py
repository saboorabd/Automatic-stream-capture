"""
One capture cycle (Playwright). On timeout it records a diagnosis in STAGE["error"],
so /api/status says WHY no stream was found.
"""

import json
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

STAGE = {"name": "idle", "since": None, "error": None}

MEDIA_HINT = re.compile(r"\.(m3u8|mpd|mp4|webm|m4s|ts|key)(\?|#|$)|mpegurl|video", re.IGNORECASE)

PLAY_VIDEO_JS = """
() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; v.play().catch(() => {}); }
}
"""

SNAPSHOT_JS = """
() => ({
  title: document.title,
  videos: document.querySelectorAll('video').length,
  iframes: document.querySelectorAll('iframe').length,
  buttons: [...document.querySelectorAll('button')].slice(0, 10).map(
    b => ((b.className || '') + ' | ' + (b.innerText || '').trim().slice(0, 30))
  )
})
"""


def set_stage(name):
    STAGE["name"] = name
    STAGE["since"] = datetime.now().isoformat(timespec="seconds")
    print(f"[{datetime.now():%H:%M:%S}] [capture] {name}", flush=True)


def click_play_everywhere(page, selectors):
    for frame in page.frames:
        for selector in selectors:
            try:
                frame.click(selector, timeout=1500)
                print(f"[capture] clicked '{selector}'", flush=True)
                return True
            except Exception:
                continue
    return False


def start_video_everywhere(page):
    for frame in page.frames:
        try:
            frame.evaluate(PLAY_VIDEO_JS)
        except Exception:
            pass


def diagnose(main_status, frames, requests_seen, media_seen, page_info):
    if main_status in (401, 403, 429, 503):
        return f"Site blocked the server (HTTP {main_status}). Likely datacenter IP or bot detection."
    if media_seen and not requests_seen.get("m3u8"):
        return "Media requests seen, but no .m3u8. Check stream_pattern (maybe mp4 or DASH)."
    if requests_seen["total"] < 5 and frames <= 1:
        return "Page loaded but player did not start. Check play_selectors in the page snapshot."
    if frames > 1:
        return "Player is inside an iframe. Stream request was not seen in any frame."
    return "Player loaded, but no matching stream request came through."


def capture_once(cfg):
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    timeout_s = cfg["capture_timeout_s"]
    selectors = cfg.get("play_selectors", [])

    found = []
    hosts = set()
    media_seen = []
    main_status = {"code": None}
    requests_seen = {"total": 0, "m3u8": 0}
    STAGE["error"] = None

    with sync_playwright() as p:
        set_stage("launching_chrome")
        browser = p.chromium.launch(
            headless=cfg["headless"],
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context()
        set_stage("chrome_started")

        def on_request(request):
            requests_seen["total"] += 1
            host = urlparse(request.url).hostname
            if host:
                hosts.add(host)
            if MEDIA_HINT.search(request.url) and len(media_seen) < 10:
                media_seen.append(request.url[:160])
            if pattern.search(request.url):
                requests_seen["m3u8"] += 1
                if not found:
                    found.append(request.url)
                    print(f"[capture] Found stream: {request.url}", flush=True)

        def on_response(response):
            if response.request.resource_type == "document" and main_status["code"] is None:
                main_status["code"] = response.status

        context.on("request", on_request)
        context.on("response", on_response)
        page = context.new_page()

        try:
            set_stage("opening_page")
            try:
                page.goto(cfg["page_url"], wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                STAGE["error"] = f"page load: {type(exc).__name__}: {exc}"
                print(f"[capture] page load warning: {exc}", flush=True)
            set_stage("page_loaded")

            set_stage("waiting_for_stream")
            deadline = time.monotonic() + timeout_s
            clicked = False
            while time.monotonic() < deadline and not found:
                if not clicked:
                    clicked = click_play_everywhere(page, selectors)
                start_video_everywhere(page)
                page.wait_for_timeout(1000)

            if found:
                set_stage("stream_found")
                return found[0]

            frames = len(page.frames)
            try:
                page_info = page.evaluate(SNAPSHOT_JS)
            except Exception as exc:
                page_info = {"error": str(exc)}

            diag = {
                "diagnosis": diagnose(main_status["code"], frames, requests_seen, media_seen, page_info),
                "main_http_status": main_status["code"],
                "frames": frames,
                "requests_seen": requests_seen["total"],
                "hosts": sorted(hosts)[:15],
                "media_candidates": media_seen,
                "page": page_info,
                "clicked_play": clicked,
            }
            print(f"[capture] DIAGNOSIS: {json.dumps(diag, ensure_ascii=False)}", flush=True)
            STAGE["error"] = json.dumps(diag, ensure_ascii=False)
            set_stage("timeout_no_stream")
            return None

        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass
            set_stage("idle")
