"""
Warm-browser capture. Four sources, checked every second, in priority order:
  1. Video Link Extractor Pro : its getLinks message (verified at startup).
  2. Stream Detector / M3U8 Sniffer : links shown in their popup, when they show any.
  3. Network sniffing : every request the browser makes, in every frame.
  4. Page source scan : m3u8 URLs written inside the HTML/JS/JSON of every frame.
The first source with a match wins, and its newest match is returned.
Works even if no extension loads, because sources 3 and 4 need none.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright
from display import ensure_display

STAGE = {"name": "idle", "since": None, "error": None}

EXTENSIONS_ROOT = Path(os.environ.get("EXTENSIONS_ROOT", "/opt/extensions"))
VLE_NAME = "Video Link Extractor Pro"
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
SEEN_CAP = 500
SCAN_EVERY = 3  # Heavy scans (page source, store popups) run every 3rd loop.

M3U8_RE = re.compile(r'https?:[^\s"\'<>]*?\.m3u8[^\s"\'<>]*', re.IGNORECASE)

_session = {"pw": None, "context": None, "page": None, "vle_popup": None,
            "scan_tab": None, "profile": None, "exts": [], "vle_ready": False, "seen": None}

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

IS_EXTENSION_PAGE_JS = """
() => typeof chrome !== 'undefined' && !!chrome.runtime && !!chrome.runtime.id
"""


def set_stage(name):
    STAGE["name"] = name
    STAGE["since"] = datetime.now().isoformat(timespec="seconds")
    print(f"[{datetime.now():%H:%M:%S}] [capture] {name}", flush=True)


def ext_id_for_path(path):
    """Chrome's ID for an unpacked extension: first 32 hex chars of SHA-256(path), a-p alphabet."""
    digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def discover_extensions():
    found = []
    if not EXTENSIONS_ROOT.is_dir():
        return found
    for d in sorted(EXTENSIONS_ROOT.iterdir()):
        manifest_path = d / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        action = data.get("action") or data.get("browser_action") or {}
        name = data.get("name", d.name)
        found.append({
            "dir": str(d),
            "name": name,
            "id": ext_id_for_path(str(d)),
            "popup": action.get("default_popup") or "popup.html",
            "is_vle": name == VLE_NAME,
        })
    return found


def resolve_ids(context, exts):
    """Replace computed IDs with the real ones, read from each live service worker."""
    for worker in context.service_workers:
        try:
            name = worker.evaluate("() => chrome.runtime.getManifest().name")
            real_id = worker.url.split("/")[2]
        except Exception:
            continue
        for ext in exts:
            if ext["name"] == name:
                ext["id"] = real_id


def verify_vle(popup, ext):
    try:
        popup.goto(f"chrome-extension://{ext['id']}/{ext['popup']}", timeout=15_000)
        return bool(popup.evaluate(IS_EXTENSION_PAGE_JS))
    except Exception as exc:
        print(f"[capture] video link extractor not reachable: {type(exc).__name__}", flush=True)
        return False


def safe_eval(page, script):
    try:
        return page.evaluate(script)
    except Exception:
        return None


def collect_vle(popup):
    result = safe_eval(popup, COLLECT_LINKS_JS) or []
    return [i["url"] for i in result if i.get("url")]


def scan_store_popups(scan_tab, exts):
    hits = []
    for ext in exts:
        if ext["is_vle"]:
            continue
        try:
            scan_tab.goto(f"chrome-extension://{ext['id']}/{ext['popup']}",
                          wait_until="domcontentloaded", timeout=8_000)
            text = scan_tab.evaluate(
                "() => document.documentElement.innerHTML + ' ' + document.body.innerText"
            )
            hits.extend(M3U8_RE.findall(text.replace("\\/", "/")))
        except Exception:
            continue
    return hits


def scan_page_sources(page):
    found = []
    for frame in page.frames:
        try:
            html = frame.content().replace("\\/", "/")
        except Exception:
            continue
        found.extend(M3U8_RE.findall(html))
    return found


def open_session():
    ensure_display()
    exts = discover_extensions()
    profile = tempfile.mkdtemp(prefix="vle-")
    pw = sync_playwright().start()
    try:
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if exts:
            paths = ",".join(e["dir"] for e in exts)
            args += [f"--disable-extensions-except={paths}", f"--load-extension={paths}"]

        kwargs = {
            "headless": False,  # Extensions need headful Chromium (Xvfb on Render).
            "ignore_default_args": ["--disable-extensions"],
            "args": args,
            "viewport": {"width": 1280, "height": 720},
        }
        if PROXY_URL:
            kwargs["proxy"] = {"server": PROXY_URL}

        context = pw.chromium.launch_persistent_context(profile, **kwargs)
        seen = []

        def on_request(request):
            if "m3u8" in request.url.lower():
                seen.append(request.url)
                if len(seen) > SEEN_CAP:
                    del seen[: SEEN_CAP // 2]

        context.on("request", on_request)

        page = context.new_page()
        vle_popup = context.new_page()
        scan_tab = context.new_page()

        vle_ready = False
        if exts:
            time.sleep(3)  # Let extension service workers start.
            resolve_ids(context, exts)
            vle = next((e for e in exts if e["is_vle"]), None)
            if vle:
                vle_ready = verify_vle(vle_popup, vle)
            print(f"[capture] extensions: {[e['name'] for e in exts]} | vle_ready={vle_ready}",
                  flush=True)
        else:
            print("[capture] no extensions found, using network and page-source sources only",
                  flush=True)
    except Exception:
        shutil.rmtree(profile, ignore_errors=True)
        pw.stop()
        raise

    _session.update(pw=pw, context=context, page=page, vle_popup=vle_popup,
                    scan_tab=scan_tab, profile=profile, exts=exts,
                    vle_ready=vle_ready, seen=seen)
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
        _session[key] = [] if key == "exts" else (False if key == "vle_ready" else None)


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
        vle_popup = _session["vle_popup"]
        scan_tab = _session["scan_tab"]
        exts = _session["exts"]
        vle_ready = _session["vle_ready"]

        if vle_ready:
            safe_eval(vle_popup, CLEAR_LINKS_JS)
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
        loop_no = 0
        store_hits, source_hits = [], []
        vle_hits = []

        while time.monotonic() < deadline:
            if not clicked:
                clicked = click_play(page, cfg.get("play_selectors", []))
            start_video(page)

            loop_no += 1
            if loop_no % SCAN_EVERY == 1:
                source_hits = scan_page_sources(page)
                store_hits = scan_store_popups(scan_tab, exts)

            vle_hits = collect_vle(vle_popup) if vle_ready else []
            network_hits = list(_session["seen"])

            # Priority order: first source with a match wins.
            for group in (vle_hits, store_hits, network_hits, source_hits):
                matches = [u for u in group if pattern.search(u)]
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
            "diagnosis": "No matching stream from any source. Check play_selectors or page_url.",
            "page_title": title,
            "frames": len(page.frames),
            "clicked_play": clicked,
            "vle_ready": vle_ready,
            "vle_links": len(vle_hits),
            "network_m3u8_seen": len(_session["seen"]),
            "page_source_m3u8": len(source_hits),
            "store_popup_m3u8": len(store_hits),
            "extensions_loaded": [e["name"] for e in exts],
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
