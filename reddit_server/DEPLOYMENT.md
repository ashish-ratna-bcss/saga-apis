# Deployment Guide

How to run this service in every environment from a laptop to a container
orchestrator, and the operational tradeoffs each one carries. Pair this with
[INTEGRATION.md](INTEGRATION.md) (how a calling application talks to the
service) and [README.md](README.md) (what the service is and its endpoint
reference).

The service is a single stateless-by-design FastAPI process. It has two Reddit
transports (OAuth API and public RSS) and, as of the RSS shared-gateway
feature, a small amount of **process-local** in-memory state (a rate-limit
bucket, a short-TTL feed cache, a per-caller request counter). That state is
the one thing that changes how you should think about scaling -- see
["Scaling and multiple instances"](#8-scaling-and-multiple-instances) before
you put more than one process behind a load balancer.

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Configuration reference](#2-configuration-reference)
3. [Running locally (no container)](#3-running-locally-no-container)
4. [Docker (single container)](#4-docker-single-container)
5. [Docker Compose](#5-docker-compose)
6. [Bare metal / VM with systemd](#6-bare-metal--vm-with-systemd)
7. [Reverse proxy and TLS](#7-reverse-proxy-and-tls)
8. [Scaling and multiple instances](#8-scaling-and-multiple-instances)
9. [Kubernetes](#9-kubernetes)
10. [Managed container platforms (Render, Railway, Fly.io, Cloud Run, ECS)](#10-managed-container-platforms)
11. [Health checks and probes](#11-health-checks-and-probes)
12. [Logging and observability](#12-logging-and-observability)
13. [Secrets management](#13-secrets-management)
14. [Zero-downtime deploys and graceful shutdown](#14-zero-downtime-deploys-and-graceful-shutdown)
15. [Verifying a deployment](#15-verifying-a-deployment)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Prerequisites

- Python 3.11+ (3.12 recommended; `pyproject.toml` pins `requires-python >= 3.11`).
- Nothing else runtime-required: no database, no Redis, no message queue. See
  `requirements.txt` -- FastAPI, uvicorn, pydantic/pydantic-settings, httpx,
  python-dotenv. That's the whole runtime dependency tree.
- **Only if you want the OAuth transport** (`/api/reddit/*`, everything except
  `/rss/*`): a Reddit app at <https://www.reddit.com/prefs/apps> (see README.md
  section 3). The RSS transport (`/api/reddit/rss/*`) needs no Reddit
  credentials at all and works with a completely empty `.env`.
- Docker 24+ if you're using the container path (this guide's Docker/Compose
  sections were built and verified against Docker 29).

---

## 2. Configuration reference

Every setting is an environment variable, loaded via `pydantic-settings` from
the process environment or a `.env` file in the working directory (see
`app/core/config.py`). Copy [.env.example](.env.example) as your starting
point in every deployment method below -- it documents every variable inline.

**Minimum viable `.env` for RSS-only deployments** (no Reddit account needed):

```bash
API_KEYS=
```

That's it. Every `REDDIT_RSS_*` setting has a working default.

**Minimum viable `.env` to also enable the OAuth transport:**

```bash
REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
REDDIT_USER_AGENT=your-app-name/1.0.0 (by /u/your_username)
```

**Recommended for anything beyond local development:**

```bash
API_KEYS=<generate with the command below, one per consuming application>
```

```bash
python -c "from app.core.security import generate_api_key; print(generate_api_key())"
```

If `API_KEYS` is left empty, every `/api/reddit/*` route is open to any caller
that can reach the port. The service logs a loud warning at startup when this
is the case -- treat that warning as a deployment blocker for anything beyond
localhost.

Full variable groups (all documented with defaults in `.env.example`):

| Group | Variables | Required? |
|---|---|---|
| Application | `APP_NAME`, `ENVIRONMENT`, `HOST`, `PORT`, `LOG_LEVEL`, `LOG_JSON`, `CORS_ORIGINS` | No, all default |
| Service auth | `API_KEYS` | No (open if unset) -- **set for anything but local dev** |
| Reddit OAuth | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`, `REDDIT_USER_AGENT`, `REDDIT_OAUTH_BASE_URL`, `REDDIT_API_BASE_URL`, `REDDIT_REQUEST_TIMEOUT_SECONDS`, `REDDIT_MAX_RETRIES`, `REDDIT_RETRY_BASE_DELAY_SECONDS`, `REDDIT_MAX_RETRY_DELAY_SECONDS`, `REDDIT_MAX_RATE_LIMIT_WAIT_SECONDS` | Only for `/api/reddit/*` (non-RSS) |
| Reddit RSS | `REDDIT_RSS_USER_AGENT`, `REDDIT_RSS_USER`, `REDDIT_RSS_FEED`, `REDDIT_RSS_TIMEOUT_SECONDS`, `REDDIT_RSS_MAX_RETRIES`, `REDDIT_RSS_RETRY_BASE_DELAY_SECONDS`, `REDDIT_RSS_MAX_RESPONSE_BYTES`, `REDDIT_RSS_EVENT_THRESHOLD` | No, all default |
| RSS shared-gateway infra | `REDDIT_RSS_GLOBAL_RATE`, `REDDIT_RSS_GLOBAL_BURST`, `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS`, `REDDIT_RSS_CACHE_TTL_SECONDS`, `REDDIT_RSS_CACHE_MAX_ENTRIES`, `REDDIT_RSS_CLIENT_LIMIT`, `REDDIT_RSS_CLIENT_WINDOW_SECONDS`, `REDDIT_RSS_CLIENT_LIMIT_MAX_TRACKED_CLIENTS` | No, all default |

Validate your configuration is loadable before deploying anywhere:

```bash
python -c "from app.core.config import Settings; print(Settings().public_summary())"
```

This never touches the network -- it only proves the `.env`/environment
parses and passes pydantic's validators (e.g. that `REDDIT_OAUTH_BASE_URL`
actually points at `reddit.com`).

---

## 3. Running locally (no container)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # edit as needed -- see section 2
python run.py                    # serves http://0.0.0.0:8000
```

```bash
python run.py --reload           # auto-reload on file changes, development only
python run.py --host 127.0.0.1 --port 9000
python run.py --workers 4        # see section 8 before using this in production
```

Verify:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

Run the test suite before deploying any change:

```bash
python -m pytest
python -m ruff check app tests
python -m mypy app
```

---

## 4. Docker (single container)

A `Dockerfile` ships at the repo root -- single-stage, `python:3.12-slim`,
non-root user, a built-in `HEALTHCHECK` against `/health`, single `uvicorn`
worker by default (see section 8 for why).

Build and run:

```bash
docker build -t reddit-provider-service:latest .

docker run -d \
  --name reddit-service \
  --env-file .env \
  -p 8000:8000 \
  reddit-provider-service:latest
```

Verify:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
docker inspect --format='{{json .State.Health}}' reddit-service
```

This exact build/run/verify sequence was tested against Docker 29 while
writing this guide: the image builds in one stage from `requirements.txt`,
`/health` returns `{"status":"ok"}` immediately, and the container's own
`HEALTHCHECK` reports `"healthy"` within its `start_period`.

To pass configuration without a `.env` file (e.g. from a secrets manager or
CI):

```bash
docker run -d --name reddit-service -p 8000:8000 \
  -e API_KEYS=rsk_xxx \
  -e REDDIT_CLIENT_ID=xxx \
  -e REDDIT_CLIENT_SECRET=xxx \
  -e REDDIT_USER_AGENT="your-app/1.0.0 (by /u/you)" \
  reddit-provider-service:latest
```

Rebuilding after a code change:

```bash
docker build -t reddit-provider-service:latest .
docker stop reddit-service && docker rm reddit-service
docker run -d --name reddit-service --env-file .env -p 8000:8000 reddit-provider-service:latest
```

---

## 5. Docker Compose

`docker-compose.yml` ships at the repo root -- one service, builds from the
local `Dockerfile`, reads `.env`, publishes port 8000, carries the same
healthcheck as the raw Docker path.

```bash
cp .env.example .env    # edit as needed
docker compose up -d --build
docker compose logs -f
docker compose ps       # shows health status once the start_period elapses
```

```bash
docker compose down             # stop and remove the container
docker compose down --rmi local # also remove the built image
```

`docker compose config` validates the file without starting anything --
useful in CI to catch a broken compose file before deploying.

If you need a reverse proxy alongside it (see section 7), add it as a second
service in the same compose file and put it on the same Docker network; this
guide keeps the shipped `docker-compose.yml` to the app alone so it stays
usable as a drop-in dependency of your own compose stack.

---

## 6. Bare metal / VM with systemd

For a VM or dedicated server without a container runtime.

```bash
sudo useradd --system --home /opt/reddit-service --shell /usr/sbin/nologin reddit-service
sudo mkdir -p /opt/reddit-service
sudo cp -r . /opt/reddit-service/
cd /opt/reddit-service
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env   # edit in place, then lock it down:
sudo chown -R reddit-service:reddit-service /opt/reddit-service
sudo chmod 600 /opt/reddit-service/.env
```

Create `/etc/systemd/system/reddit-service.service`:

```ini
[Unit]
Description=Reddit Provider Service
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=reddit-service
Group=reddit-service
WorkingDirectory=/opt/reddit-service
EnvironmentFile=/opt/reddit-service/.env
ExecStart=/opt/reddit-service/.venv/bin/uvicorn reddit_app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
# Give in-flight requests time to finish on SIGTERM (uvicorn's default graceful
# shutdown); raise if you see requests cut off during deploys.
TimeoutStopSec=30

# Hardening (optional but recommended)
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/reddit-service
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reddit-service
sudo systemctl status reddit-service
journalctl -u reddit-service -f
```

Deploying a new version:

```bash
cd /opt/reddit-service
sudo git pull    # or rsync/copy your build
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart reddit-service
```

`EnvironmentFile=` reads the same `KEY=value` format as `.env`; no code
changes needed to move between this and the Docker path.

---

## 7. Reverse proxy and TLS

This service does not terminate TLS itself -- put a reverse proxy in front of
it for anything internet-facing. Example nginx config:

```nginx
server {
    listen 443 ssl http2;
    server_name reddit-service.example.com;

    ssl_certificate     /etc/letsencrypt/live/reddit-service.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/reddit-service.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 65s;   # RSS calls can wait behind the shared rate
                                  # limiter -- see REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS
    }
}

server {
    listen 80;
    server_name reddit-service.example.com;
    return 301 https://$host$request_uri;
}
```

**Important caveat, read before relying on per-caller RSS rate limiting**: the
per-caller limiter (`REDDIT_RSS_CLIENT_LIMIT`, see INTEGRATION.md section 10)
keys on `request.client.host`, the immediate TCP peer. Behind the nginx config
above, that will be `127.0.0.1` (or the proxy's address) for every request
unless you also proxy the real client IP into the app -- which this service
does **not** currently parse (`X-Forwarded-For` is not read; there is no
trusted-proxy allowlist). Behind a reverse proxy, every caller passing through
it shares one bucket in the per-caller limiter. This is documented, not a bug:
add `X-Forwarded-For` trust with an allowlist if you need real per-IP
isolation behind a proxy -- it isn't implemented in this codebase today.

`proxy_read_timeout` above matters specifically for `/api/reddit/rss/*`: a
request can legitimately wait behind the shared global rate limiter for up to
`REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS` (default 55s) before this service itself
gives up with `504 REDDIT_RSS_QUEUE_TIMEOUT`. Set your proxy's read timeout
comfortably above that value, or callers will see a proxy-level 504 instead of
this service's own structured error.

---

## 8. Scaling and multiple instances

**Read this before running more than one process.**

This service has three kinds of state, and they behave differently under
scale-out:

| State | Where | Behavior across multiple processes |
|---|---|---|
| OAuth access token | `RedditRestClient` (in-memory) | Each process re-authenticates independently. Fine -- Reddit's OAuth rate limit is per-token, and each process gets its own token. |
| RSS shared-gateway budget | `TokenBucket`/`FeedCache`/`ClientRateLimiter` (in-memory, process-local) | **Each process gets its own budget, cache, and per-caller counters.** N processes fronting the same Reddit-facing IP means N× the intended Reddit RSS request rate, N separate feed caches (less cache-hit benefit), and a per-caller limit that resets per-process a caller happens to land on. |
| Everything else | None -- every OAuth/RSS call reads live from Reddit | No cross-process concern; the service has no database. |

**What this means concretely:**

- `uvicorn --workers N` (N > 1), multiple Docker replicas, or multiple
  Kubernetes pod replicas each spin up an independent Python process with its
  own copy of the RSS rate limiter and cache.
- If your RSS call volume is low enough that Reddit's own ~1 req/min-per-IP
  ceiling isn't the binding constraint, running multiple workers/replicas is
  fine -- you just don't get the "N callers, 1 Reddit request" guarantee
  described in INTEGRATION.md's shared-gateway architecture; each process
  independently paces itself to ~1 req/min, so the *aggregate* could be
  N req/min against Reddit.
- If you need one true shared budget across multiple processes, this
  codebase does not implement that today (deliberately: no Redis or similar
  dependency was introduced for a single-process deployment). The
  `TokenBucket`/`FeedCache`/`ClientRateLimiter` classes (`app/reddit/rate_limiter.py`,
  `app/reddit/feed_cache.py`, `app/core/rate_limit.py`) are the seams where a
  Redis-backed (or similar) implementation would plug in if you outgrow a
  single process.

**Recommendation:**

- **Default: run exactly one process.** `docker run` / `docker-compose.yml`
  as shipped do this. For systemd, don't add `--workers`. This gives you the
  full shared-gateway guarantee with zero extra infrastructure.
- **If you need horizontal scaling for the OAuth transport's throughput**
  (which has no such caveat -- Reddit's OAuth rate limit is per-token, not
  per-IP), scale that independently, e.g. by running RSS-heavy traffic
  through one pinned instance/replica and letting OAuth traffic load-balance
  freely -- or simply accept N× the RSS budget if your deployment's RSS call
  volume is low relative to what N processes would use.
- **If you must run N replicas and need the strict shared budget preserved**,
  that requires implementing a distributed backend for the three classes
  above before scaling out -- treat it as a prerequisite, not an
  afterthought.

---

## 9. Kubernetes

A minimal Deployment + Service. Adjust the image reference, namespace, and
resource requests for your cluster.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: reddit-service
spec:
  replicas: 1 # see section 8 before raising this
  selector:
    matchLabels:
      app: reddit-service
  template:
    metadata:
      labels:
        app: reddit-service
    spec:
      containers:
        - name: reddit-service
          image: your-registry/reddit-provider-service:latest
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef:
                name: reddit-service-config
            - secretRef:
                name: reddit-service-secrets
          readinessProbe:
            httpGet:
              path: /ready
              port: 8000
            initialDelaySeconds: 2
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 15
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
---
apiVersion: v1
kind: Service
metadata:
  name: reddit-service
spec:
  selector:
    app: reddit-service
  ports:
    - port: 80
      targetPort: 8000
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: reddit-service-config
data:
  ENVIRONMENT: "production"
  LOG_JSON: "true"
  REDDIT_USER_AGENT: "your-app/1.0.0 (by /u/you)"
---
apiVersion: v1
kind: Secret
metadata:
  name: reddit-service-secrets
type: Opaque
stringData:
  API_KEYS: "rsk_xxx"
  REDDIT_CLIENT_ID: "xxx"
  REDDIT_CLIENT_SECRET: "xxx"
```

Notes:

- `replicas: 1` is deliberate -- see section 8. Raise it only after reading
  that section's tradeoffs.
- `readinessProbe` uses `/ready`, which checks Reddit-OAuth configuration
  (not a live network call) -- cheap enough for frequent probing and
  meaningful for load-balancer inclusion. `livenessProbe` uses `/health`,
  pure process liveness, never Reddit-dependent -- correct for "should
  Kubernetes restart this pod," which should never trigger on a Reddit outage.
- Put the OAuth credentials and `API_KEYS` in the `Secret`, everything else
  can live in the `ConfigMap`.
- Apply with `kubectl apply -f reddit-service.yaml` (save the above as one
  file, or split it -- both work with `kubectl apply -f`).

---

## 10. Managed container platforms

These platforms all build from the shipped `Dockerfile` with minimal
platform-specific config. In each case: connect the repo (or push the image),
set the environment variables from section 2, set the health check path to
`/health`, and keep to a single instance/replica per section 8 unless you've
accepted that tradeoff.

**Render / Railway** -- "New Web Service" from this repo, they auto-detect the
`Dockerfile`. Set environment variables in the dashboard. Set the health check
path to `/health`. Both default to one instance unless you explicitly scale.

**Fly.io**

```bash
fly launch --no-deploy   # generates fly.toml from the Dockerfile
fly secrets set API_KEYS=rsk_xxx REDDIT_CLIENT_ID=xxx REDDIT_CLIENT_SECRET=xxx
fly deploy
```

In the generated `fly.toml` (field names shift between `flyctl` versions --
check `fly config show` against your installed version), set roughly:

```toml
[http_service]
  internal_port = 8000

[[http_service.checks]]
  path = "/health"
  interval = "30s"
  timeout = "5s"

[[vm]]
  # keep min_machines_running / count at 1 unless you've read section 8
```

**Google Cloud Run**

```bash
gcloud builds submit --tag gcr.io/YOUR_PROJECT/reddit-provider-service
gcloud run deploy reddit-provider-service \
  --image gcr.io/YOUR_PROJECT/reddit-provider-service \
  --port 8000 \
  --set-env-vars API_KEYS=rsk_xxx,REDDIT_USER_AGENT="your-app/1.0.0 (by /u/you)" \
  --set-secrets REDDIT_CLIENT_ID=reddit-client-id:latest,REDDIT_CLIENT_SECRET=reddit-client-secret:latest \
  --min-instances 1 --max-instances 1   # see section 8: Cloud Run's autoscaling
                                          # spins up independent instances, each
                                          # with its own RSS shared-gateway state
```

Cloud Run's health checking uses the container's own startup/liveness probes
if you configure them, or TCP by default -- add an explicit HTTP probe against
`/health` in the service YAML if you want the same behavior as the Kubernetes
example.

**AWS ECS/Fargate** -- push the image to ECR, define a task definition with
one container (port 8000, environment from section 2, or `secrets` referencing
Secrets Manager/SSM Parameter Store for `API_KEYS`/`REDDIT_CLIENT_SECRET`),
and a container health check:

```json
"healthCheck": {
  "command": ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)\""],
  "interval": 30,
  "timeout": 5,
  "retries": 3,
  "startPeriod": 10
}
```

Set the service's `desiredCount` to 1 unless you've accepted the section 8
tradeoff.

---

## 11. Health checks and probes

Two distinct endpoints, deliberately different in cost and meaning:

| Endpoint | Checks | Network call to Reddit? | Use for |
|---|---|---|---|
| `GET /health` | Process is up and serving | No | Liveness probe / "should this instance be restarted" |
| `GET /ready` | `REDDIT_CLIENT_ID`/`SECRET`/`USER_AGENT` are set | No | Readiness probe / "should this instance receive traffic" |
| `GET /api/reddit/status` | Live OAuth token acquisition + (for password grant) identity check | **Yes** | Manual/on-demand diagnostics, not a probe -- see below |

Do **not** wire `/api/reddit/status` into a liveness or readiness probe: it
makes a real request to Reddit on every call, so a transient Reddit outage or
rate limit would cause your orchestrator to kill/drain healthy instances for a
problem entirely outside this service's control. Use `/health` and `/ready`
for probes; use `/api/reddit/status` for a human (or an alerting job on a
longer interval) checking whether the configured credentials still work.

Neither `/health` nor `/ready` requires `X-API-Key` even when `API_KEYS` is
set -- load balancers and orchestrators never need a secret to probe them.

---

## 12. Logging and observability

Set `LOG_JSON=true` in any environment with a log aggregator (CloudWatch,
Loki, Datadog, ELK, etc.) -- logs become newline-delimited JSON instead of
human-readable text, with structured fields the aggregator can index. Set
`LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`/`ERROR`) per environment; `INFO` is a
reasonable production default, `WARNING` for a quieter one.

Every log line already has secrets scrubbed by a redaction filter attached at
the root logger (`app/core/logging.py`) -- the Reddit client secret, account
password, OAuth access token, RSS feed token, and every configured `API_KEYS`
value are registered and replaced with `***REDACTED***` even if a third-party
library (httpx, uvicorn) tries to log them. You do not need to configure this;
it's on by default.

There is no metrics/counters framework in this service (no Prometheus
client, no StatsD) -- observability is structured logs only, by design (no new
dependency was introduced for it). If you need metrics, the natural place to
add a counter/histogram library is where the RSS shared-gateway logs already
emit structured `extra={...}` fields (`app/reddit/rss_client.py`,
`app/reddit/feed_cache.py`, `app/core/rate_limit.py`) -- log-based metrics
(parsing JSON logs into counters in your aggregator) work today with zero
code changes.

---

## 13. Secrets management

- **Never commit `.env`.** It's already in `.gitignore`; double-check before
  your first commit in a fork.
- Local dev: `.env` file, `python-dotenv` loads it automatically.
- Docker: `--env-file .env` or `-e KEY=value`; for anything beyond local
  testing, prefer your platform's secret store (Docker Swarm secrets, a
  mounted secrets volume) over baking values into an image or compose file
  committed to source control.
- Kubernetes: a `Secret` object (see section 9) -- never a `ConfigMap` for
  `API_KEYS`/`REDDIT_CLIENT_SECRET`/`REDDIT_PASSWORD`/`REDDIT_RSS_FEED`.
- Cloud platforms: use the platform's secret manager (GCP Secret Manager, AWS
  Secrets Manager/SSM, Fly.io secrets, Render/Railway's secret env vars) --
  every example in section 10 shows the platform-native way to do this rather
  than a plain environment variable where a secret manager is available.
- `REDDIT_RSS_FEED` (the RSS rate-limit workaround token, see INTEGRATION.md
  section 10) is not a login credential, but this service treats it as a
  secret anyway (never logged, never returned by any endpoint) -- handle it
  with the same care as `REDDIT_CLIENT_SECRET` in whichever secret store you
  use.
- Rotate `API_KEYS` by adding the new key alongside the old one (it's a
  comma-separated list), updating callers, then removing the old key --
  zero-downtime rotation, no code changes needed.

---

## 14. Zero-downtime deploys and graceful shutdown

`uvicorn` handles `SIGTERM` gracefully by default: it stops accepting new
connections and waits for in-flight requests to finish (up to its own
timeout) before exiting. This service's own shutdown hook
(`app/main.py`'s `lifespan`) closes both Reddit HTTP clients cleanly on the
way out -- no dangling connections.

Practical implications per deployment method:

- **systemd**: `TimeoutStopSec=30` in the unit file (section 6) gives
  in-flight requests up to 30s to finish before systemd force-kills the
  process on `stop`/`restart`. Raise it if you see requests cut off, e.g. if
  callers frequently hit the RSS shared-gateway's up-to-55s queue wait
  (`REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS`).
- **Docker**: `docker stop` sends `SIGTERM` then `SIGKILL` after a grace
  period (default 10s -- pass `-t 30` to `docker stop` to extend it, matching
  the systemd guidance above).
- **Kubernetes**: set `terminationGracePeriodSeconds` on the pod spec (default
  30s) to at least `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS` if you rely on the RSS
  shared-gateway's queueing behavior; Kubernetes also removes the pod from
  Service endpoints before sending `SIGTERM` when a readiness probe is
  configured, so in-flight requests naturally drain without new ones arriving.
- **Rolling deploys**: since there's no database and no cross-instance shared
  state beyond the process-local RSS gateway state (section 8), a standard
  rolling deploy (new instance up and ready, old instance drained and
  stopped) is safe with no special migration/coordination step. The RSS
  shared-gateway cache/rate-limiter simply reset to empty on the new
  instance -- no warm-up procedure required, just a possible brief dip in
  cache-hit rate immediately after a deploy.

---

## 15. Verifying a deployment

Run this sequence after any deploy, regardless of method:

```bash
BASE_URL=https://reddit-service.example.com   # or http://localhost:8000

curl -sf "$BASE_URL/health"
# {"status":"ok"}

curl -sf "$BASE_URL/ready"
# {"status":"ok","reddit_configured":true}   -- or "not_configured" if you're RSS-only

curl -sf "$BASE_URL/api/reddit/status" -H "X-API-Key: $API_KEY"
# {"connected":true,"authorized":true,"account":{"username":"a****t"}}
# only meaningful if the OAuth transport is configured -- see section 11 on
# why this must never be a probe, only a manual/on-demand check

curl -sf -X POST "$BASE_URL/api/reddit/rss/monitor" \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"query": "test", "limit": 1}'
# {"source":"reddit","transport":"rss","authenticated":false, ...} -- proves
# the RSS transport can reach Reddit; may occasionally 403/429 on Reddit's
# own anti-scraping/rate limiting even when this service is healthy (see
# INTEGRATION.md section 10) -- retry once before concluding the deploy failed
```

If `API_KEYS` is unset, drop the `X-API-Key` header from the last two calls.

---

## 16. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/ready` returns `"not_configured"` | `REDDIT_CLIENT_ID`/`SECRET`/`USER_AGENT` unset | Expected for RSS-only deployments; set them if you need `/api/reddit/*` (non-RSS) |
| `/api/reddit/*` (non-RSS) returns `503 REDDIT_NOT_CONFIGURED` | Same as above | Same as above |
| `/api/reddit/*` returns `401 UNAUTHORIZED` | `API_KEYS` is set and the caller didn't send a matching `X-API-Key` | Send the header, or confirm the key matches one in the comma-separated list |
| Every request seems to share one RSS rate-limit/cache window unexpectedly | Behind a reverse proxy, or running multiple workers/replicas | See sections 7 and 8 |
| RSS calls intermittently return `403 REDDIT_RSS_FORBIDDEN` or `429 REDDIT_RSS_RATE_LIMITED` | Reddit's own anti-scraping/rate limiting on the RSS host -- not this service failing | Expected occasionally per INTEGRATION.md section 10; set `REDDIT_RSS_USER`/`REDDIT_RSS_FEED` to raise the ceiling |
| RSS calls return `504 REDDIT_RSS_QUEUE_TIMEOUT` | The shared process-local budget is oversubscribed for this deployment's call volume | Lower call volume, raise `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS`, or reconsider scaling per section 8 |
| Startup log warns "API authentication is DISABLED" | `API_KEYS` is empty | Set it before exposing the service beyond localhost |
| Docker `HEALTHCHECK` never turns healthy | Container can't reach itself on `127.0.0.1:8000`, or the app crashed on startup | `docker logs <container>` for the actual startup error |
| Requests cut off mid-response during a deploy | Graceful-shutdown grace period shorter than an in-flight request needs | See section 14 -- extend the relevant timeout |
