"""
One capture cycle:
  1. Opens a fresh incognito Chrome via SeleniumBase (UC mode).
  2. Loads the page and tries to start playback.
  3. Reads the browser's resource timing list for a matching .m3u8 URL.
  4. Closes the browser, so no cookies or session data survive.
"""

import re
import time

from selenium.webdriver.common.by import By
from seleniumbase import Driver


def capture_once(cfg: dict) -> str | None:
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    driver = Driver(uc=True, headless=cfg["headless"], incognito=True)

    try:
        driver.get(cfg["page_url"])

        # Try the configured play buttons first.
        for selector in cfg.get("play_selectors", []):
            try:
                driver.find_element(By.CSS_SELECTOR, selector).click()
                break
            except Exception:
                continue

        # Fallback: start the <video> element directly (muted, so autoplay is allowed).
        driver.execute_script(
            "const v = document.querySelector('video');"
            "if (v) { v.muted = true; v.play().catch(() => {}); }"
        )

        deadline = time.monotonic() + cfg["capture_timeout_s"]
        while time.monotonic() < deadline:
            # Every network resource the page has loaded so far, including fetch/XHR.
            urls = driver.execute_script(
                "return performance.getEntriesByType('resource').map(e => e.name);"
            )
            for url in urls:
                if pattern.search(url):
                    return url
            time.sleep(1)

        return None
    finally:
        driver.quit()
