# Build context is the Saga-APIs repo root (not unified_api/ itself), since
# this image needs all four existing service packages as siblings:
#   docker build -f unified_api/Dockerfile -t saga-unified-api .
FROM python:3.12-slim

WORKDIR /srv

# Union of what each service's own Dockerfile already installs:
# curl (Bluweb), build-essential + dnsutils + whois (OSINT's domain_recon
# adapter shells out to nslookup/whois; build-essential for sdist-only deps).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential dnsutils whois \
    && rm -rf /var/lib/apt/lists/*

COPY unified_api/requirements.txt ./unified_api/requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
    -r unified_api/requirements.txt
# Browser fallback (Bluweb) needs Playwright's bundled Chromium + OS deps.
RUN playwright install --with-deps chromium

# Each existing package, unmodified — mirrors the host layout so every
# service's BASE_DIR-relative .env/data path resolution keeps working as-is.
COPY Bluweb/bluweb_app ./Bluweb/bluweb_app
COPY Bluweb/migrations ./Bluweb/migrations
COPY Bluweb/alembic.ini ./Bluweb/alembic.ini
COPY OSINT/osint_app ./OSINT/osint_app
COPY OSINT/alembic ./OSINT/alembic
COPY OSINT/alembic.ini ./OSINT/alembic.ini
COPY reddit_server/reddit_app ./reddit_server/reddit_app
COPY telegram_poc/telegram_app ./telegram_poc/telegram_app
COPY unified_api/app ./unified_api/app

WORKDIR /srv/unified_api
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
