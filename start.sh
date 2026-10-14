#!/bin/sh
# Starts a virtual screen (Xvfb) for headful Chromium, waits until it is ready, then runs the app.
set -e

if [ ! -S /tmp/.X11-unix/X99 ]; then
  Xvfb :99 -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  i=0
  while [ ! -S /tmp/.X11-unix/X99 ] && [ "$i" -lt 40 ]; do
    sleep 0.5
    i=$((i + 1))
  done
fi

export DISPLAY=:99
echo "[start] DISPLAY=$DISPLAY, starting app"
exec python app.py
