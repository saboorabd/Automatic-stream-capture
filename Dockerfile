FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

RUN apt-get update && apt-get install -y --no-install-recommends xvfb unzip curl \
    && rm -rf /var/lib/apt/lists/*

# Download the real extension source from GitHub at build time.
RUN curl -sL https://github.com/jrcramos/video_link_extractor/archive/refs/heads/main.zip -o /tmp/ext.zip \
    && unzip -q /tmp/ext.zip -d /opt \
    && mv /opt/video_link_extractor-main /opt/extension \
    && rm /tmp/ext.zip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV PYTHONUNBUFFERED=1 EXTENSION_DIR=/opt/extension
# Extensions need a real (headful) Chromium, so it runs on a virtual display.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x720x24", "python", "app.py"]
