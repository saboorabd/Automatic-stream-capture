FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the browser extensions into /opt/extensions/<name>/ at build time.
# Failures are reported and skipped, so the build still succeeds.
COPY fetch_extensions.py /tmp/fetch_extensions.py
RUN python3 /tmp/fetch_extensions.py

COPY . .
ENV PYTHONUNBUFFERED=1 EXTENSIONS_ROOT=/opt/extensions DISPLAY=:99

# Extensions need headful Chromium, so run it on a virtual display, then the web app.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 -nolisten tcp & sleep 3 && exec python app.py"]
