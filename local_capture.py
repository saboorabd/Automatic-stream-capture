"""
Local capture. Runs on YOUR PC (home IP, visible Chrome).
Finds the .m3u8 for page_url, then pushes it to the Render web app.
Render only plays it.

Setup:
    pip install playwright requests
    playwright install chromium
Run:
    python local_capture.py
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

CFG = json.loads(Path(__file__).with_name("local_config.json").read_text(encoding="utf-8"))
PATTERN = re.compile(CFG["stream_pattern"], re.IGNORECASE)
PLAY_VIDEO_JS = """
() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; v.play().catch(() => {}); }
}
"""


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def click_play(page):
    for frame in page.frames:
        for selector in CFG["play_selectors"]:
            try:
                frame.click(selector, timeout=1000)
                return True
            except Exception:
                continue
    return False


def start_video(page):
    for frame in page.frames:
        try:
            frame.evaluate(PLAY_VIDEO_JS)
        except Exception:
            pass


def capture_once():
    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=CFG["headless"],
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context()  # Fresh session, like incognito.

        def on_request(request):
            # Context-level listener: sees requests from every page and iframe.
            if not found and PATTERN.search(request.url):
                found.append(request.url)

        context.on("request", on_request)
        page = context.new_page()

        try:
            page.goto(CFG["page_url"], wait_until="domcontentloaded", timeout=60_000)
            deadline = time.monotonic() + CFG["capture_timeout_s"]
            clicked = False
            while time.monotonic() < deadline and not found:
                if not clicked:
                    clicked = click_play(page)
                start_video(page)
                page.wait_for_timeout(1000)
            return found[0] if found else None
        finally:
            context.close()
            browser.close()


def push_to_render(stream_url):
    response = requests.post(
        CFG["render_url"].rstrip("/") + "/api/target",
        json={"stream_url": stream_url},
        timeout=20,
    )
    response.raise_for_status()


def main():
    last_pushed = None
    log("Local capture started. Ctrl+C to stop.")
    while True:
        try:
            url = capture_once()
            if not url:
                log("No stream seen this cycle.")
            elif url == last_pushed:
                log("Same stream as last cycle.")
            else:
                log(f"Found: {url[:100]}")
                push_to_render(url)
                last_pushed = url
                log("Pushed to Render. Web player will switch.")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"Error: {type(exc).__name__}: {exc}")
        time.sleep(CFG["refresh_interval_s"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
