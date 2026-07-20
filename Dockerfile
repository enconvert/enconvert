# Open-source EnConvert gateway — self-host image.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# LibreOffice (office -> PDF via unoserver) + fonts + curl. Chromium's own
# system dependencies are installed by `playwright install --with-deps` below,
# which picks the correct package names for this base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice \
      fonts-liberation fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
      curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt && pip install unoserver
RUN python -m playwright install --with-deps chromium

COPY . .

EXPOSE 8010

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
