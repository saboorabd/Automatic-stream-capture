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
    driver.execute_script(
        "const v = document.querySelector('video');"
        "if (v) { v.muted = true; v.play().catch(() => {}); }"
    )


def snapshot(driver):
    return driver.execute_script(
        "return {"
        "  title: document.title,"
        "  videos: document.querySelectorAll('video').length,"
        "  iframes: document.querySelectorAll('ifr
