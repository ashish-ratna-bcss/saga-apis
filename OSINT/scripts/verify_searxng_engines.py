#!/usr/bin/env python3
"""Query the local SearxNG instance once per candidate engine and record
which are configured/reachable/returned_results/failed/blocked. Prints a
table -- run manually, not part of the automated test suite (hits a real
network service by design). See README "Local Development".
"""
import json
import sys
import urllib.parse
import urllib.request

SEARXNG_URL = "http://127.0.0.1:8890"
ENGINES = ["duckduckgo", "startpage", "brave", "wikipedia", "bing", "mojeek", "google"]
QUERY = "OSINT investigation phone number lookup"


def query_engine(engine: str) -> dict:
    params = {"q": QUERY, "format": "json", "engines": engine}
    url = f"{SEARXNG_URL}/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "osint-verify-script/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        return {"engine": engine, "status": "unreachable", "detail": str(exc)}

    unresponsive = body.get("unresponsive_engines", [])
    for entry in unresponsive:
        # entries are [engine_name, error_type] or [engine_name, error_type, suspended]
        if entry and entry[0] == engine:
            error_type = entry[1] if len(entry) > 1 else "unknown"
            status = "blocked" if "captcha" in str(error_type).lower() or "suspend" in str(error_type).lower() else "failed"
            return {"engine": engine, "status": status, "detail": error_type}

    results = body.get("results", [])
    infoboxes = body.get("infoboxes", [])
    if results or infoboxes:
        return {"engine": engine, "status": "returned_results", "detail": f"{len(results)} results, {len(infoboxes)} infoboxes"}
    return {"engine": engine, "status": "reachable_no_results", "detail": "0 results for this query"}


def main() -> int:
    print(f"{'engine':<12} {'status':<20} detail")
    print("-" * 70)
    any_working = False
    for engine in ENGINES:
        result = query_engine(engine)
        print(f"{result['engine']:<12} {result['status']:<20} {result['detail']}")
        if result["status"] == "returned_results":
            any_working = True
    return 0 if any_working else 1


if __name__ == "__main__":
    sys.exit(main())
