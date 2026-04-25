# Observability

Three pieces, one goal — when prod misbehaves we should know within
seconds and find the cause without ssh'ing into the box.

## 1. Sentry

**Why:** silent prod exceptions = silent missed revenue. We want a stack
trace with request context the moment an unhandled exception fires.

**Where:** `djangoProject/settings/base.py` initialises the SDK only when
`SENTRY_DSN` is set. Empty DSN = no-op, so dev / CI / preview stay quiet
without code changes.

**Setup (per environment):**
1. https://sentry.io → Create project (Django).
2. Copy the DSN.
3. Add to env (NOT to git):
   - Local dev `.env`: `SENTRY_DSN=https://...`
   - GitHub Actions secret `SENTRY_DSN` if CI is wanted (usually not — CI noise)
   - Production deploy env (the VPS / hosting platform's env panel).
4. Set `SENTRY_ENVIRONMENT=production` on prod, `staging` on staging.
5. Inject the deploy SHA: `SENTRY_RELEASE=$GITHUB_SHA` in CI / deploy.

**Knobs:**
- `SENTRY_TRACES_SAMPLE_RATE` — fraction of requests to trace (0.1 = 10%).
- Health endpoints (`/api/v1/health/*`) are explicitly excluded from
  tracing — they fire every few seconds and would dominate the quota.
- `send_default_pii=False` is locked on for 152-ФЗ compliance. Don't flip
  without legal review.

**Custom filtering:** `_sentry_before_send` in `base.py` is the place to
strip new sensitive fields if the data model evolves (e.g. when memory
arch lands and `pregnancy_status` enters event payloads). Today it's a
no-op pass-through.

## 2. Structured logging

**Why:** plain-text logs are unparseable past a hundred lines per second.
JSON one-line-per-event lets aggregators (Datadog, Loki, ELK) filter,
group, and alert without regex acrobatics.

**Two formats, one config:**
- `LOG_FORMAT=human` — readable plaintext, default for dev. The format
  prepends timestamp + level + request_id + logger so a tail-grep flow
  works.
- `LOG_FORMAT=json` — `python-json-logger` output, default for prod (set
  unconditionally in `prod.py`).

**Levels:** `LOG_LEVEL` env (default `INFO`). Django framework loggers
(`django`, `django.request`) stay at `INFO`/`WARNING` regardless to
avoid spamming on routine requests.

**Request ID injection:** every log record carries the active
`X-Request-ID` value via `RequestIDFilter` (see below). Records emitted
outside a request (mgmt commands, startup) get `request_id="-"` so the
formatter never blows up on a missing field.

## 3. X-Request-ID middleware

**What it does:**
- Reads the incoming `X-Request-ID` header, falls back to a fresh UUID.
- Stores the value on `request.request_id` and in a thread-local that
  the LOGGING filter reads.
- Echoes the value back in the response.

**Why we honour the incoming header:** mobile apps and any upstream
gateway already trace requests with their own correlation id. If we
ignore it the trace breaks at our boundary; honouring it lets a single
id thread through their logs and ours.

**Order:** middleware sits first in `MIDDLEWARE` so every other layer —
auth, app-type guard, view, renderer, exception handler — sees the same
id and emits log lines under it.

## 4. Health endpoints

| URL | Purpose | Interval | What it checks |
|---|---|---|---|
| `/api/v1/health/` | Liveness (loadbalancer) | seconds | DB ping, cache round-trip |
| `/api/v1/health/ready/` | Readiness (deploy + on-call) | once at boot, on alert | liveness + migrations applied |

Both return JSON with per-check status; 200 when all healthy, 503 when
any check fails. Loadbalancer / k8s probe should hit `/health/`. Deploy
scripts should poll `/health/ready/` until 200 before flipping traffic.

## 5. What this PR does NOT add (yet)

- **Outbox lag check** in readiness — pending PR3 (Celery + Redis).
- **Worker metrics** — Celery surfaces them once running.
- **Alerting rules** — these live in Sentry / Grafana, not in the repo.
- **APM tracing instrumentation** beyond Sentry's defaults — overkill at
  current traffic volumes.

## Alerting recommendations (for the on-call rota when it exists)

| Signal | Trigger | Severity | Action |
|---|---|---|---|
| Sentry: unhandled exception, prod | New issue, any | P2 | Investigate within business hours |
| Sentry: 5xx rate > 1% over 5 min | Rolling window | P1 | Page on-call |
| Liveness 503 for 30s | LB callback | P1 | Page on-call |
| Readiness 503 for 5 min | Deploy never completed | P2 | Look at deploy log |
| `auth_sensitive` throttle hits per minute > 100 | (PR2 logs) | P3 | Investigate brute-force |
