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

        # Timeout: log what the page actually contains, so we can fix selectors.
        info = driver.execute_script(
            "return {"
            "  title: document.title,"
            "  videos: document.querySelectorAll('video').length,"
            "  iframes: document.querySelectorAll('iframe').length,"
            "  buttons: [...document.querySelectorAll('button')].slice(0, 15)"
            "    .map(b => (b.className || '') + ' | ' + (b.innerText || '').trim().slice(0, 30)),"
            "  resources: performance.getEntriesByType('resource').length"
            "};"
        )
        print(f"[capture] page snapshot: {info}", flush=True)
        STAGE["error"] = f"no stream. snapshot: {info}"

        set_stage("timeout_no_stream")
        return None
