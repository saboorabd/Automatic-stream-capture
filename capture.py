"""
One capture cycle using Playwright:
  1. Launches a fresh Chromium context (no cookies, no storage).
  2. Opens the page. Watches requests from ALL frames, including iframes.
  3. Clicks play and starts <video> in every frame.
  4. Returns the first request URL that matches stream_pattern.
  5. Closes everything.
"""

import re
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

STAGE = {"name": "idle", "since": None, "error": None}

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
  buttons: [...document.querySelectorAll('button')].slice(0, 15).map(
    b => (b.className || '') + ' | ' + (b.innerText || '').trim().slice(0, 30)
  )
})
"""


def set_stage(name):
    STAGE["name"] = name
    STAGE["since"] = datetime.now().isoformat(timespec="seconds")
    print(f"[{datetime.now():%H:%M:%S}] [capture] {name}", flush=True)


def click_play_everywhere(page, selectors):
    """Tries each selector in every frame. Returns True if one click worked."""
    for frame in page.frames:
        for selector in selectors:
            try:
                frame.click(selector, timeout=1500)
                print(f"[capture] clicked '{selector}' in frame {frame.url[:80]}", flush=True)
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


def capture_once(cfg):
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    timeout_s = cfg["capture_timeout_s"]
    selectors = cfg.get("play_selectors", [])
    found = []
    STAGE["error"] = None

    with sync_playwright() as p:
        set_stage("launching_chrome")
        browser = p.chromium.launch(
            headless=cfg["headless"],
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = browser.new_context()  # Fresh incognito-like context.
        set_stage("chrome_started")

        def on_request(request):
            if not found and pattern.search(request.url):
                found.append(request.url)
                print(f"[capture] Found stream: {request.url}", flush=True)

        # Context-level listener sees requests from every page and iframe.
        context.on("request", on_request)
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

            info = {"frames": len(page.frames)}
            try:
                info["page"] = page.evaluate(SNAPSHOT_JS)
            except Exception as exc:
                info["page_error"] = str(exc)
            print(f"[capture] snapshot: {info}", flush=True)
            STAGE["error"] = f"no stream. snapshot: {info}"
            set_stage("timeout_no_stream")
            return None

        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass
            set_stage("idle")
