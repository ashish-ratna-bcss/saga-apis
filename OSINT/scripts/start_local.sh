#!/usr/bin/env bash
# Single command to bring up the whole no-Docker local stack: SearxNG
# (background, only if not already running) + FastAPI (foreground -- SQLite
# mode runs the worker in-process, see app/main.py's lifespan, so no
# separate worker process is needed here; see README "Local Development").
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x .venv/bin/python ]; then
    echo "Main app venv missing. Run:" >&2
    echo "  python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

SEARXNG_PORT=8890
if curl -s -o /dev/null "http://127.0.0.1:${SEARXNG_PORT}/"; then
    echo "SearxNG already running on :${SEARXNG_PORT}"
else
    if [ ! -x searxng-src/venv/bin/python ]; then
        echo "SearxNG not installed. Run ./scripts/setup_searxng.sh first." >&2
        exit 1
    fi
    echo "Starting SearxNG on :${SEARXNG_PORT} (log: searxng.log)..."
    (
        export SEARXNG_SETTINGS_PATH="$ROOT_DIR/searxng-src/settings.yml"
        # shellcheck disable=SC1091
        source searxng-src/venv/bin/activate
        exec python -m searx.webapp
    ) > searxng.log 2>&1 &
    SEARXNG_PID=$!
    sleep 2
    if ! kill -0 "$SEARXNG_PID" 2>/dev/null; then
        echo "SearxNG failed to start -- see searxng.log for the error." >&2
        exit 1
    fi
    echo "SearxNG started (pid $SEARXNG_PID)."
fi

echo "Starting FastAPI (SQLite mode, in-process worker) -- Ctrl+C to stop."
exec ./.venv/bin/python -m osint_app.main
