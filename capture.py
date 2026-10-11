"""
One capture cycle:
  1. Opens a fresh incognito Chrome (system Chrome from Dockerfile).
  2. Loads the page and tries to start playback.
  3. Reads the browser's resource list for a matching .m3u8 URL.
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


def set_stage(name: str) -> None:
    STAGE["name"] = name
    STAGE["since"] = datetime.now().isoformat(timespec="seconds")
    print(f"[{datetime.now():%H:%M:%S}] [capture] {name}", flush=True)


def capture_once(cfg: dict) -> str | None:
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    driver = None
    STAGE["error"] = None

    try:
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

        set_stage("opening_page")
        try:
            driver.get(cfg["page_url"])
        except Exception as exc:
            # Page load timed out or failed. Keep going: the stream may already be requested.
            STAGE["error"] = f"page load: {type(exc).__name__}: {exc}"
            print(f"[capture] page load warning: {exc}", flush=True)
        set_stage("page_loaded")

        for selector in cfg.get("play_selectors", []):
            try:
                driver.find_element(By.CSS_SELECTOR, selector).click()
                set_stage(f"clicked_{selector}")
                break
            except Exception:
                continue

        driver.execute_script(
            "const v = document.querySelector('video');"
            "if (v) { v.muted = true; v.play().catch(() => {}); }"
        )

        set_stage("waiting_for_stream")
        deadline = time.monotonic() + cfg["capture_timeout_s"]
        while time.monotonic() < deadline:
            urls = driver.execute_script(
                "return performance.getEntriesByType('resource').map(e => e.name);"
            )
            for url in urls:
                if pattern.search(url):
                    set_stage("stream_found")
                    print(f"[capture] Found stream: {url}", flush=True)
                    return url
            time.sleep(1)

        set_stage("timeout_no_stream")
        return None

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
