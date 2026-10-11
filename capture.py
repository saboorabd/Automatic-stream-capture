"""
One capture cycle:
  1. Launches a fresh incognito Chrome (system Chrome from Dockerfile).
  2. Opens the page and tries to start playback.
  3. Looks for a matching .m3u8 request in the browser's resource list.
  4. Closes the browser, so no session data survives.

STAGE shows the current step, so /api/status tells where it is stuck.
"""

import re
import time
import traceback
from datetime import datetime

from selenium.webdriver.common.by import By
from seleniumbase import Driver

STAGE = {"name": "idle", "since": None, "error": None}

PLAY_VIDEO_JS = """
const v = document.querySelector('video');
if (v) { v.muted = true; v.play().catch(() => {}); }
"""

RESOURCE_URLS_JS = """
return performance.getEntriesByType('resource').map(e => e.name);
"""

SNAPSHOT_JS = """
return {
  title: document.title,
  videos: document.querySelectorAll('video').length,
  iframes: document.querySelectorAll('iframe').length,
  buttons: [...document.querySelectorAll('button')].slice(0, 15).map(
    b => (b.className || '') + ' | ' + (b.innerText || '').trim().slice(0, 30)
  ),
  resources: performance.getEntriesByType('resource').length
};
"""


def set_stage(name):
    STAGE["name"] = name
    STAGE["since"] = datetime.now().isoformat(timespec="seconds")
    print(f"[{datetime.now():%H:%M:%S}] [capture] {name}", flush=True)


def launch_chrome(cfg):
    set_stage("launching_chrome")
    driver = Driver(
        browser="chrome",
        headless=cfg["headless"],
        incognito=True,
        no_sandbox=True,
        chromium_arg=(
            "--disable-dev-shm-usage,"
            "--disable-gpu,"
            "--disable-software-rasterizer,"
            "--disable-setuid-sandbox,"
            "--autoplay-policy=no-user-gesture-required"
        ),
    )
    driver.set_page_load_timeout(60)
    set_stage("chrome_started")
    return driver


def open_page(driver, url):
    set_stage("opening_page")
    try:
        driver.get(url)
    except Exception as exc:
        # Timeout or partial load. Keep going, the stream may already be requested.
        STAGE["error"] = f"page load: {type(exc).__name__}: {exc}"
        print(f"[capture] page load warning: {exc}", flush=True)
    set_stage("page_loaded")


def click_play(driver, selectors):
    for selector in selectors:
        try:
            driver.find_element(By.CSS_SELECTOR, selector).click()
            set_stage(f"clicked_{selector}")
            return
        except Exception:
            pass


def start_video(driver):
    driver.execute_script(PLAY_VIDEO_JS)


def wait_for_stream(driver, pattern, timeout_s):
    set_stage("waiting_for_stream")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        urls = driver.execute_script(RESOURCE_URLS_JS)
        for url in urls:
            if pattern.search(url):
                set_stage("stream_found")
                print(f"[capture] Found stream: {url}", flush=True)
                return url
        time.sleep(1)

    info = driver.execute_script(SNAPSHOT_JS)
    print(f"[capture] page snapshot: {info}", flush=True)
    STAGE["error"] = f"no stream. snapshot: {info}"
    set_stage("timeout_no_stream")
    return None


def capture_once(cfg):
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    STAGE["error"] = None
    driver = None

    try:
        driver = launch_chrome(cfg)
        open_page(driver, cfg["page_url"])
        click_play(driver, cfg.get("play_selectors", []))
        start_video(driver)
        return wait_for_stream(driver, pattern, cfg["capture_timeout_s"])

    except Exception as exc:
        STAGE["error"] = f"{type(exc).__name__}: {exc}"
        print(traceback.format_exc(), flush=True)
        raise

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        set_stage("idle")
