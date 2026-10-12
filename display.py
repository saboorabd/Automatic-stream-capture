"""Starts a virtual X display (Xvfb) for headful Chromium, once per process."""

import os
import shutil
import subprocess
import time

DISPLAY = os.environ.get("XVFB_DISPLAY", ":99")
_proc = None


def ensure_display():
    global _proc
    if _proc is not None and _proc.poll() is None:
        os.environ["DISPLAY"] = DISPLAY
        return

    if shutil.which("Xvfb") is None:
        raise RuntimeError("Xvfb is not installed (apt-get install xvfb)")

    log_file = open("/tmp/xvfb.log", "w")
    _proc = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
        stdout=log_file,
        stderr=log_file,
    )
    time.sleep(2)
    if _proc.poll() is not None:
        log_file.flush()
        with open("/tmp/xvfb.log", errors="replace") as fh:
            raise RuntimeError(f"Xvfb exited: {fh.read()[-300:]}")

    os.environ["DISPLAY"] = DISPLAY
    print(f"[display] Xvfb started on {DISPLAY}", flush=True)
