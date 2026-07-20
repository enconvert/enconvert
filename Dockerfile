# Open-source Enconvert gateway — self-host image.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System dependencies:
#  - LibreOffice + unoserver: office documents -> PDF
#  - the libX*/libnss/etc. set: headless Chromium for crawl4ai/Playwright
#  - fonts + cairo/pango: faithful rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice \
      fonts-liberation fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
      libpango-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
      curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt && pip install unoserver
RUN python -m playwright install chromium

COPY . .

EXPOSE 8010

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
