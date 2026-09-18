#!/usr/bin/env bash
# Starts the native SearxNG instance set up by setup_searxng.sh. Foreground
# process -- run in its own terminal, or via scripts/start_local.sh which
# backgrounds it. Listens on 127.0.0.1:8890 (see searxng-src/settings.yml).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT_DIR/searxng-src"

if [ ! -d "$SRC_DIR/venv" ]; then
    echo "SearxNG not installed yet -- run ./scripts/setup_searxng.sh first." >&2
    exit 1
fi

export SEARXNG_SETTINGS_PATH="$SRC_DIR/settings.yml"
# shellcheck disable=SC1091
source "$SRC_DIR/venv/bin/activate"
exec python -m searx.webapp
