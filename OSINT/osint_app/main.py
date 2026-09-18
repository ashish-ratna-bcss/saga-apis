import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from osint_app import metrics
from osint_app.config import settings
from osint_app.db import SessionLocal, engine, init_db, is_sqlite
from osint_app.logging_config import configure_logging
from osint_app.models import AuditLog
from osint_app.orchestrator import start_workers, stop_workers
from osint_app.routes import investigations, lookups, search, sources, utils

configure_logging()
logger = logging.getLogger("osint.access")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if is_sqlite:
        # Postgres deployments run workers as a separate process
        # (`python -m osint_app.worker_main`) -- see README "Job queue" /
        # docker-compose.yml. The API process itself never runs OSINT
        # adapter calls in that mode, only on the SQLite dev path.
        await start_workers(settings.worker_concurrency)
    yield
    if is_sqlite:
        await stop_workers()


app = FastAPI(
    title="India OSINT Phase 1",
    description=(
        "Free-only public-source intelligence platform: "
        "identifier -> public intelligence -> entities -> relationships -> evidence -> confidence score."
    ),
    lifespan=lifespan,
)

app.include_router(investigations.router)
app.include_router(lookups.router)
app.include_router(search.router)
app.include_router(sources.router)
app.include_router(utils.router)


def _extract_investigation_id(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) >= 4 and parts[:3] == ["api", "v1", "investigations"]:
        return parts[3]
    return None


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Every response carries an X-Request-ID (echoed back if the caller
    supplied one, generated otherwise -- see STEP 17). Also writes one
    structured access-log line and one AuditLog row per request (STEP 16):
    method/path/status/duration, plus investigation_id (parsed from the
    path). Audit failures are logged, never allowed to break the actual
    response."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.monotonic()

    response = await call_next(request)

    duration_ms = (time.monotonic() - start) * 1000
    response.headers["X-Request-ID"] = request_id

    investigation_id = _extract_investigation_id(request.url.path)

    logger.info(
        "request",
        extra={
            "request_id": request_id,
            "investigation_id": investigation_id,
        },
    )

    try:
        db = SessionLocal()
        try:
            db.add(
                AuditLog(
                    request_id=request_id,
                    investigation_id=investigation_id,
                    operation=f"{request.method} {request.url.path}",
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 2),
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("failed to write audit log entry", extra={"request_id": request_id})

    return response


def _structured_error(request: Request, status_code: int, message, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": status_code,
                "message": message,
                "request_id": getattr(request.state, "request_id", None),
                "details": details,
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return _structured_error(request, exc.status_code, exc.detail)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return _structured_error(request, 422, "request validation failed", details=exc.errors())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness (vs. liveness /health): can this instance actually serve a
    request right now -- i.e. is the database reachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(exc)})
    return {"status": "ready"}


@app.get("/metrics", response_class=PlainTextResponse)
def get_metrics():
    """Prometheus text exposition format -- see STEP 18. Per-process
    counters, the standard Prometheus model (aggregation happens at the
    scraping server, not in-app)."""
    return metrics.render_prometheus_text()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("osint_app.main:app", host=settings.host, port=settings.port)
