"""
Capture using the REAL extension, with ONE warm browser kept alive between cycles.

  First cycle : launch Chromium + extension once.
  Every cycle : clear the extension's old links, reload page_url, click play,
                return the newest stream link. Much faster than relaunching.
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

_session = {"pw": None, "context": None, "page": None, "popup": None, "profile": None, "ext_id": None}

PLAY_VIDEO_JS = """
() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; v.play().catch(() => {}); }
}
"""

# Runs inside the extension popup page (has chrome.runtime / chrome.tabs).
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
    return worker.url.split("/")[2]  # chrome-extension://<id>/background.js


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
                "--autoplay-policy=no-user-gesture-required",
            ],
            "viewport": {"width": 1280, "height": 720},
        }
        if PROXY_URL:
            launch_kwargs["proxy"] = {"server": PROXY_URL}

        context = pw.chromium.launch_persistent_context(profile, **launch_kwargs)
        ext_id = wait_for_extension_id(context)
        page = context.new_page()
        popup = context.new_page()
        popup.goto(f"chrome-extension://{ext_id}/popup.html")
    except Exception:
        shutil.rmtree(profile, ignore_errors=True)
        pw.stop()
        raise

    _session.update(pw=pw, context=context, page=page, popup=popup,
                    profile=profile, ext_id=ext_id)
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


def capture_once(cfg):
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    STAGE["error"] = None

    try:
        if not _session["context"]:
            set_stage("launching_chrome")
            open_session()

        page = _session["page"]
        popup = _session["popup"]

        # Forget links from the previous cycle, so only fresh ones are returned.
        popup.evaluate(CLEAR_LINKS_JS)

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
        links = []
        while time.monotonic() < deadline:
            if not clicked:
                clicked = click_play(page, cfg.get("play_selectors", []))
            start_video(page)

            links = popup.evaluate(COLLECT_LINKS_JS)
            matches = [item["url"] for item in links if pattern.search(item["url"])]
            if matches:
                stream = matches[-1]  # Newest match.
                print(f"[capture] Found stream: {stream}", flush=True)
                set_stage("stream_found")
                return stream

            page.wait_for_timeout(1000)

        try:
            title = page.title()
        except Exception:
            title = ""
        diag = {
            "diagnosis": "Extension sniffed no matching stream. Check play_selectors or page_url.",
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

    except Exception as exc:
        # Browser may be broken. Drop it so the next cycle starts clean.
        STAGE["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[capture] failed, resetting browser: {STAGE['error']}", flush=True)
        close_session()
        raise
