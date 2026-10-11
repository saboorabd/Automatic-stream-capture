"""
One capture cycle:
  1. Opens a fresh incognito Chrome via SeleniumBase (UC mode).
  2. Loads the page and tries to start playback.
  3. Reads the browser's resource list for a matching .m3u8 URL.
  4. Closes the browser, so no session data survives.
"""

import re
import time
from datetime import datetime

from selenium.webdriver.common.by import By
from seleniumbase import Driver


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] [capture] {message}", flush=True)


def capture_once(cfg: dict) -> str | None:
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)

    log("Launching Chrome...")
    driver = Driver(
        uc=True,
        headless=cfg["headless"],
        incognito=True,
        no_sandbox=True,  # Required when running as root inside Docker.
        chromium_arg="--disable-dev-shm-usage,--disable-gpu",  # Docker /dev/shm is tiny.
    )
    log("Chrome started.")

    try:
        driver.get(cfg["page_url"])
        log(f"Opened {cfg['page_url']}")

        for selector in cfg.get("play_selectors", []):
            try:
                driver.find_element(By.CSS_SELECTOR, selector).click()
                log(f"Clicked '{selector}'")
                break
            except Exception:
                continue

        driver.execute_script(
            "const v = document.querySelector('video');"
            "if (v) { v.muted = true; v.play().catch(() => {}); }"
        )

        deadline = time.monotonic() + cfg["capture_timeout_s"]
        while time.monotonic() < deadline:
            urls = driver.execute_script(
                "return performance.getEntriesByType('resource').map(e => e.name);"
            )
            for url in urls:
                if pattern.search(url):
                    log(f"Found stream: {url}")
                    return url
            time.sleep(1)

        log("Timeout: no .m3u8 request seen.")
        return None
    finally:
        driver.quit()
        log("Chrome closed.")
