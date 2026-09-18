FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reddit_app ./reddit_app
COPY run.py .

RUN useradd --create-home --shell /usr/sbin/nologin appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

# Single worker by default -- the RSS shared-gateway rate limiter/cache/client
# limiter are process-local (see DEPLOYMENT.md "Scaling"); running more than one
# worker/replica multiplies the effective Reddit RSS budget instead of sharing
# it, unless that's an accepted tradeoff for your deployment.
CMD ["uvicorn", "reddit_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
