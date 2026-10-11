"""
One capture cycle, using the REAL extension (jrcramos/video_link_extractor).

  1. Launches Chromium with the extension loaded, in a fresh temporary profile.
  2. Opens page_url, clicks play, starts <video> in every frame.
  3. Asks the extension's background for its sniffed links (its own getLinks message).
  4. Returns the first link matching stream_pattern. The profile is deleted afterwards.

STAGE shows the current step; on timeout STAGE["error"] holds a diagnosis.
"""

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

STAGE = {"name": "idle", "since": None, "error": None}

EXT_DIR = os.environ.get("EXTENSION_DIR", "/opt/extension")
PROXY_URL = os.environ.get("PROXY_URL", "").strip()  # Optional. Leave empty if unused.

PLAY_VIDEO_JS = """
() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; v.play().catch(() => {}); }
}
"""

# Runs inside the extension's popup page, so chrome.runtime and chrome.tabs are available.
COLLECT_LINKS_JS = """
async () => {
  const self = await chrome.tabs.getCurrent();
  const tabs = await chrome.tabs.query({});
  const out = [];
  for (const t of tabs) {
    if (!t.id || (self && t.id === self.id)) continue;
    const r = await new Promise((resolve) =>
      chrome.runtime.sendMessage({ action: 'getLinks', tabId: t.id }, (res) => resolve(res))
    );
    if (r && Array.isArray(r.links)) {
      for (const l of r.links) {
        out.push({ url: l.url, referer: l.referer || r.pageUrl || '' });
      }
    }
  }
  return out;
}
"""


def set_stage(name):
    STAGE["name"] = name
    STAGE["since"] = datetime.now().isoformat(timespec="seconds")
    print(f"[{datetime.now():%H:%M:%S}] [capture] {name}", flush=True)


def wait_for_extension_id(context):
    if context.service_workers:
        worker = context.service_workers[0]
    else:
        worker = context.wait_for_event("serviceworker", timeout=30_000)
    # URL looks like chrome-extension://<id>/background.js
    return worker.url.split("/")[2]


def click_play(page, selectors):
    for frame in page.frames:
        for selector in selectors:
            try:
                frame.click(selector, timeout=1000)
                print(f"[capture] clicked '{selector}'", flush=True)
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


def capture_once(cfg):
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    STAGE["error"] = None
    profile = tempfile.mkdtemp(prefix="vle-")

    try:
        with sync_playwright() as p:
            set_stage("launching_chrome")
            launch_kwargs = {
                "headless": False,  # Extensions need headful Chromium (runs under Xvfb on Render).
                "ignore_default_args": ["--disable-extensions"],
                "args": [
                    f"--disable-extensions-except={EXT_DIR}",
                    f"--load-extension={EXT_DIR}",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                ],
                "viewport": {"width": 1280, "height": 720},
            }
            if PROXY_URL:
                launch_kwargs["proxy"] = {"server": PROXY_URL}

            context = p.chromium.launch_persistent_context(profile, **launch_kwargs)
            try:
                ext_id = wait_for_extension_id(context)
                set_stage("chrome_started")

                set_stage("opening_page")
                page = context.new_page()
                try:
                    page.goto(cfg["page_url"], wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:
                    STAGE["error"] = f"page load: {type(exc).__name__}: {exc}"
                    print(f"[capture] page load warning: {exc}", flush=True)
                set_stage("page_loaded")

                # Extension popup page: used only to send the extension its getLinks message.
                popup = context.new_page()
                popup.goto(f"chrome-extension://{ext_id}/popup.html")

                set_stage("waiting_for_stream")
                deadline = time.monotonic() + cfg["capture_timeout_s"]
                clicked = False
                links = []
                while time.monotonic() < deadline:
                    if not clicked:
                        clicked = click_play(page, cfg.get("play_selectors", []))
                    start_video(page)

                    links = popup.evaluate(COLLECT_LINKS_JS)
                    for item in links:
                        if pattern.search(item["url"]):
                            print(f"[capture] Found stream: {item['url']}", flush=True)
                            set_stage("stream_found")
                            return item["url"]

                    page.wait_for_timeout(1000)

                try:
                    title = page.title()
                except Exception:
                    title = ""
                diag = {
                    "diagnosis": "Extension sniffed no matching stream. Check play_selectors or page_url.",
                    "extension_id": ext_id,
                    "page_title": title,
                    "frames": len(page.frames),
                    "clicked_play": clicked,
                    "extension_links": len(links),
                    "sample_links": [i["url"][:120] for i in links[:5]],
                }
                STAGE["error"] = json.dumps(diag, ensure_ascii=False)
                print(f"[capture] DIAGNOSIS: {STAGE['error']}", flush=True)
                set_stage("timeout_no_stream")
                return None
            finally:
                context.close()
    except Exception as exc:
        STAGE["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[capture] failed: {STAGE['error']}", flush=True)
        raise
    finally:
        shutil.rmtree(profile, ignore_errors=True)
