from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from bluweb_app.api.v1 import crawl, health, preflight, scrape
from bluweb_app.core.config import apply_hf_token_to_environ
from bluweb_app.core.errors import APIError, api_error_handler, unhandled_exception_handler
from bluweb_app.core.logging import configure_logging, request_id_var
from bluweb_app.core.metrics import register_metric_subscribers
from bluweb_app.services.events import default_bus
from bluweb_app.services.security.url_security import URLSecurityError

configure_logging()
apply_hf_token_to_environ()

register_metric_subscribers(default_bus)

app = FastAPI(
    title="Web Intelligence Collection API",
    version="0.3.0",
    description=(
        "Self-hosted scrape/crawl API (httpx + Playwright). Returns extracted "
        "content in responses; the caller owns storage. No paid scrape APIs."
    ),
)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="docs")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


app.add_exception_handler(APIError, api_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.exception_handler(URLSecurityError)
async def url_security_error_handler(request: Request, exc: URLSecurityError):
    from bluweb_app.core.url_errors import url_security_to_api_error

    return await api_error_handler(request, url_security_to_api_error(exc))


app.include_router(health.router)
app.include_router(preflight.router, prefix="/api/v1")
app.include_router(scrape.router, prefix="/api/v1")
app.include_router(crawl.router, prefix="/api/v1")
