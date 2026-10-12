"""
Capture with ONE warm browser kept alive between cycles.

Three built-in sources, no third-party extension code needed beyond the
video_link_extractor (source 1):
  1. The video_link_extractor extension (its getLinks message).
  2. Network sniffing: every request the browser makes, in every frame.
  3. Page source scan: m3u8 URLs written inside the HTML/JS/JSON of each frame.
Whichever finds a match first wins. The newest match is returned.
"""

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

STAGE = {"name": "idle", "since": None, "error": None}

EXT_DIR = os.environ.get("EXTENSION_DIR", "/opt/extension")
PROXY_URL = os.environ.get("PROXY_URL", "").strip()  # Optional.
SEEN_CAP = 500
SOURCE_SCAN_EVERY = 3  # Scan page source every 3rd loop (about every 3 seconds).

M3U8_IN_TEXT = re.compile(r'https?:[^\s"\'<>]*?\.m3u8[^\s"\'<>]*', re.IGNORECASE)

_session = {"pw": None, "context": None, "page": None, "popup": None,
            "profile": None, "ext_id": None, "seen": None}

PLAY_VIDEO_JS = """
() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; v.play().catch(() => {}); }
}
"""

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
      for (const l of r.links) out.push({ url: l.url });
    }
  }
  return out;
}
"""

CLEAR_LINKS_JS = """
async () => {
  const self = await chrome.tabs.getCurrent();
  const tabs = await chrome.tabs.query({});
  for (const t of tabs) {
    if (!t.id || (self && t.id === self.id)) continue;
    await new Promise((resolve) =>
      chrome.runtime.sendMessage({ action: 'clearLinks', tabId: t.id }, () => resolve())
    );
  }
  return true;
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
    return worker.url.split("/")[2]


def open_session():
    profile = tempfile.mkdtemp(prefix="vle-")
    pw = sync_playwright().start()
    try:
        launch_kwargs = {
            "headless": False,  # Extensions need headful Chromium (Xvfb on Render).
            "ignore_default_args": ["--disable-extensions"],
            "args": [
                f"--disable-extensions-except={EXT_DIR}",
                f"--load-extension={EXT_DIR}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--autoplay-policy=no-user-gesture-required",
            ],
            "viewport": {"width": 1280, "height": 720},
        }
        if PROXY_URL:
            launch_kwargs["proxy"] = {"server": PROXY_URL}

        context = pw.chromium.launch_persistent_context(profile, **launch_kwargs)
        seen = []

        def on_request(request):
            if "m3u8" in request.url.lower():
                seen.append(request.url)
                if len(seen) > SEEN_CAP:
                    del seen[: SEEN_CAP // 2]

        context.on("request", on_request)

        ext_id = wait_for_extension_id(context)
        page = context.new_page()
        popup = context.new_page()
        popup.goto(f"chrome-extension://{ext_id}/popup.html")
    except Exception:
        shutil.rmtree(profile, ignore_errors=True)
        pw.stop()
        raise

    _session.update(pw=pw, context=context, page=page, popup=popup,
                    profile=profile, ext_id=ext_id, seen=seen)
    set_stage("chrome_started")


def close_session():
    try:
        if _session["context"]:
            _session["context"].close()
    except Exception:
        pass
    try:
        if _session["pw"]:
            _session["pw"].stop()
    except Exception:
        pass
    if _session["profile"]:
        shutil.rmtree(_session["profile"], ignore_errors=True)
    for key in _session:
        _session[key] = None


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


def scan_page_sources(page):
    """Source 3: m3u8 URLs written inside the HTML/JS/JSON of every frame."""
    found = []
    for frame in page.frames:
        try:
            html = frame.content().replace("\\/", "/")  # Undo JSON-escaped slashes.
        except Exception:
            continue
        found.extend(M3U8_IN_TEXT.findall(html))
    return found


def capture_once(cfg):
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    STAGE["error"] = None

    try:
        if not _session["context"]:
            set_stage("launching_chrome")
            open_session()

        page = _session["page"]
        popup = _session["popup"]

        popup.evaluate(CLEAR_LINKS_JS)
        _session["seen"].clear()

        set_stage("opening_page")
        try:
            page.goto(cfg["page_url"], wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            STAGE["error"] = f"page load: {type(exc).__name__}: {exc}"
            print(f"[capture] page load warning: {exc}", flush=True)
        set_stage("page_loaded")

        set_stage("waiting_for_stream")
        deadline = time.monotonic() + cfg["capture_timeout_s"]
        clicked = False
        ext_links = []
        source_hits = []
        loop_no = 0
        while time.monotonic() < deadline:
            if not clicked:
                clicked = click_play(page, cfg.get("play_selectors", []))
            start_video(page)

            loop_no += 1
            if loop_no % SOURCE_SCAN_EVERY == 1:
                source_hits = scan_page_sources(page)

            ext_links = popup.evaluate(COLLECT_LINKS_JS)
            candidates = (
                [item["url"] for item in ext_links]   # Source 1: extension
                + list(_session["seen"])              # Source 2: network
                + source_hits                         # Source 3: page source
            )
            matches = [u for u in candidates if pattern.search(u)]
            if matches:
                stream = matches[-1]
                print(f"[capture] Found stream: {stream}", flush=True)
                set_stage("stream_found")
                return stream

            page.wait_for_timeout(1000)

        try:
            title = page.title()
        except Exception:
            title = ""
        diag = {
            "diagnosis": "No matching stream from extension, network, or page source. Check play_selectors or page_url.",
            "page_title": title,
            "frames": len(page.frames),
            "clicked_play": clicked,
            "extension_links": len(ext_links),
            "network_m3u8_seen": len(_session["seen"]),
            "page_source_m3u8": len(source_hits),
            "sample_links": [i["url"][:120] for i in ext_links[:3]],
        }
        STAGE["error"] = json.dumps(diag, ensure_ascii=False)
        print(f"[capture] DIAGNOSIS: {STAGE['error']}", flush=True)
        set_stage("timeout_no_stream")
        return None

    except Exception as exc:
        STAGE["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[capture] failed, resetting browser: {STAGE['error']}", flush=True)
        close_session()
        raise
