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
| `reschedule.unversioned_command` share of reschedule volume | > owner-set threshold, sustained | P3 | Review §6 rollout readiness |
| `booking.auto_complete.pass` **absent** for > 1h | Beat or worker is not running the sweep at all | P2 | Check celery_beat / celery_worker; the task fires every 15 min and logs on every tick, including the ticks where it does nothing |
| `booking.auto_complete.rows_failed` | Any | P2 | A visit happened, the sweep could not close it, and no later tick retries harder — see the per-row ERROR for the appointment id |
| `booking.auto_complete.pass … elapsed_unconfirmed=N` | N > 0, sustained | P2 | Visits are elapsing without ever reaching CONFIRMED — the lifecycle is broken upstream of completion, not in the sweep |

## 5a. The visit-completion sweep (DRF-1064 / DRF-1048)

`appointments.tasks.auto_complete_elapsed_bookings` closes confirmed
visits that elapsed and that nobody closed by hand. It is registered in
`CELERY_BEAT_SCHEDULE` unconditionally and gated by
`BOOKING_AUTO_COMPLETE_ENABLED` + `BOOKING_AUTO_COMPLETE_NOT_BEFORE`.

**Every tick writes exactly one line**, whatever it did or refused to do:

```
booking.auto_complete.pass ran=false reason=disabled candidates=0 ...
booking.auto_complete.pass ran=true reason=ok candidates=0 completed=0
  skipped=0 failed=0 cutoff=… not_before=…
  elapsed_unconfirmed=1 below_floor=1
```

That is deliberate, and it is the fix DRF-1048 turned out to need. The
sweep previously logged only when it closed or failed on something, so a
log with no `booking.auto_complete` lines in it was equally consistent
with beat not firing, the feature gate being closed, and the sweep
running and matching nothing — three different problems with three
different fixes. **Silence now means one thing: the task is not
running.** Everything else says which of the remaining cases it is:

- `reason=disabled` / `reason=no_floor` — gated off, one of the two
  ignition keys is missing.
- `candidates=0` with `elapsed_unconfirmed=N` — visits elapsed inside
  the window but never reached `CONFIRMED`; the sweep is correct to skip
  them and the problem is upstream.
- `candidates=0` with `below_floor=N` — the standing backlog older than
  the floor, drained deliberately via
  `manage.py complete_elapsed_backlog`.
- `cutoff` / `not_before` — the window actually swept, so a timezone or
  grace-period bug is readable from the line instead of inferred.

## 6. Temporary compatibility flags

Flags kept for backward compatibility with older clients, each with an
explicit metric and removal criterion — tracked here so they don't
silently become permanent.

### `RESCHEDULE_MOBILE_UNVERSIONED_ALLOWED` (`djangoProject/settings/base.py`)

**Risk while `True` (default, current state):** the mobile reschedule
endpoint accepts a request that omits `expected_version` (pre-Wave-1
app builds never send it). Two such requests race with zero
lost-update protection — the PostgreSQL advisory lock
(`appointments/infrastructure/db_locks.py`) only serialises concurrent
writes against the *same physical slot*; it does not stop the second
of two sequential unversioned reschedules from silently overwriting
the first's result. Flagged in code review 2026-08-03.

**Metric:** `RescheduleBookingService._execute_atomic` logs
`reschedule.unversioned_command` (with `booking_id`, `basis`,
`current_version`) every time this path is taken — structured JSON in
prod (§2), so an aggregator query can turn it into
"unversioned requests ÷ total reschedule requests" per day without a
code change.

**Rollout plan:**
1. **Now — data collection.** Flag stays `True`; the metric above runs
   in production and establishes a real baseline (today: unknown, no
   prior instrumentation existed for this).
2. **Threshold decision (owner).** Once the baseline is visible, the
   owner sets the acceptable ceiling (starting proposal: ≤1% of
   reschedule volume) and cross-checks it against mobile app-store
   version-adoption data — a low log rate that's actually "one whale
   client polling in a retry loop" reads differently from "5% of real
   users on old builds."
3. **Flip.** When the metric has stayed at/below that threshold for 14
   consecutive days AND mobile confirms no supported app-store build
   predates the version field, set
   `RESCHEDULE_MOBILE_UNVERSIONED_ALLOWED = False` in a normal deploy.
   Omitted-version requests then get `400 EXPECTED_VERSION_REQUIRED`
   (`EXPECTED_VERSION_REQUIRED` in `core/errors.py`) instead of
   executing unprotected.
4. **Post-flip watch.** Monitor the `EXPECTED_VERSION_REQUIRED` 400
   rate for a week. A rate matching (or below) the pre-flip metric
   confirms the threshold call was right. A spike means the baseline
   undercounted real traffic — revert with the same one-line change
   and re-open data collection.

**Removal criterion:** step 3 above — an objective, metric-driven
trigger rather than a fixed calendar date, since no adoption data
exists yet to justify picking one.
