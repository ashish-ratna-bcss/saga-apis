"""Unified API shell — mounts the four existing services under one process.

Each of Bluweb/OSINT/reddit_server/telegram_poc's own FastAPI ``app`` object is
mounted as-is (Starlette sub-application mounting): every existing route,
exception handler, and middleware keeps working unchanged, and each app's own
unprefixed /health becomes /<namespace>/health for free. Business logic lives
entirely in the four existing packages (bluweb_app/osint_app/reddit_app/
telegram_app) — nothing is duplicated here.

Starlette does not auto-run a mounted sub-app's own lifespan, so the parent
app's lifespan explicitly enters each sub-app's own lifespan context via
AsyncExitStack — the standard pattern for composing multiple FastAPI apps'
startup/shutdown into one process.

See ~/.claude/plans/cozy-noodling-cook.md for the milestone this implements.
"""
from __future__ import annotations

import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _service_dir in ("Bluweb", "OSINT", "reddit_server", "telegram_poc"):
    _path = str(REPO_ROOT / _service_dir)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from bluweb_app.main import app as bluweb_app  # noqa: E402
from osint_app.main import app as osint_app  # noqa: E402
from reddit_app.main import app as reddit_app  # noqa: E402
from telegram_app.main import app as telegram_app  # noqa: E402

SUB_APPS = {
    "/scrape": bluweb_app,
    "/osint": osint_app,
    "/reddit": reddit_app,
    "/telegram": telegram_app,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for sub_app in SUB_APPS.values():
            await stack.enter_async_context(sub_app.router.lifespan_context(sub_app))
        yield


app = FastAPI(title="Saga Unified API", lifespan=lifespan)

for _prefix, _sub_app in SUB_APPS.items():
    app.mount(_prefix, _sub_app)
