"""One-shot milestone-1 smoke test: import the unified app, run its merged
lifespan via TestClient, hit each namespace's health endpoint, print what
started. Not a pytest suite (per the plan, full characterization tests are a
later phase) — the smallest thing that fails if the mounting/lifespan wiring
is broken. Run with: unified_api/.venv/bin/python unified_api/smoke_test.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import SUB_APPS, app  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {name} {detail}")


try:
    with TestClient(app) as client:
        check("lifespan startup", True, "(merged lifespan entered without raising)")

        for prefix in SUB_APPS:
            try:
                resp = client.get(f"{prefix}/health")
                check(f"GET {prefix}/health", resp.status_code == 200, f"-> {resp.status_code} {resp.text[:100]}")
            except Exception as exc:  # noqa: BLE001
                check(f"GET {prefix}/health", False, f"raised {exc!r}")

        # OSINT and reddit_server additionally expose /ready
        for prefix in ("/osint", "/reddit", "/telegram"):
            try:
                resp = client.get(f"{prefix}/ready")
                check(f"GET {prefix}/ready", resp.status_code in (200, 503), f"-> {resp.status_code} {resp.text[:100]}")
            except Exception as exc:  # noqa: BLE001
                check(f"GET {prefix}/ready", False, f"raised {exc!r}")

        # Bluweb's readiness route is nested under /health/ready
        try:
            resp = client.get("/scrape/health/ready")
            check("GET /scrape/health/ready", resp.status_code in (200, 503), f"-> {resp.status_code} {resp.text[:100]}")
        except Exception as exc:  # noqa: BLE001
            check("GET /scrape/health/ready", False, f"raised {exc!r}")

        # mounted docs sanity — each sub-app's own OpenAPI schema still serves
        for prefix in SUB_APPS:
            try:
                resp = client.get(f"{prefix}/openapi.json")
                check(f"GET {prefix}/openapi.json", resp.status_code == 200, f"-> {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                check(f"GET {prefix}/openapi.json", False, f"raised {exc!r}")

    check("lifespan shutdown", True, "(merged lifespan exited without raising)")

except Exception:
    print("--- FATAL: exception outside TestClient context ---")
    traceback.print_exc()
    results.append(("fatal", False, "see traceback above"))

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
