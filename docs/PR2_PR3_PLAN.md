# PR2 + PR3 — Detailed Implementation Plan

**Date:** 2026-04-24
**Status:** Plan (awaiting approval before implementation)
**Context:** Phase 2 Lane A foundation — continues after PR1 (secret hygiene + test tooling shipped at commit `d49925f`)
**Owner:** Claude (CC+gstack)
**Total est. effort:** ~2.5 hours CC+gstack (PR2 ~60min + PR3 ~90min)

---

## Executive Summary

| PR | Scope | Why | Files touched (est.) | Effort (CC) |
|---|---|---|---|---|
| **PR2** | Sentry + structured logging + health endpoints + request ID | Observability **before** Celery — no silent failures | ~12 | 60 min |
| **PR3** | Celery + Redis + Outbox worker + django-redis cache | Event-driven arch activated (currently OutboxEvent rows pile up, no dispatch) | ~14 | 90 min |

**Ordering rationale** (from eng review A3): Observability must ship **before** Celery. If Celery worker crashes on launch, Sentry tells us; without Sentry it's silent. Also Redis + django-redis cache config from PR3 benefits from structured logging already in place.

---

## Current State Baseline (after PR1)

Что уже работает:
- ✅ Coverage tooling (pytest-cov) — baseline 83.6%
- ✅ Test factories deps installed (factory-boy, Faker, freezegun) — not yet wired
- ✅ Secret hygiene (.mcp.json gitignored)
- ✅ OutboxEvent model exists (appointments/models.py:393) — rows accumulate, no dispatcher
- ✅ Booking engine writes OutboxEvent entries in CreateBookingService, CancelRescheduleService
- ✅ docker-compose: web + db (postgres:16) + minio + minio-init

Что НЕ работает:
- ❌ OutboxEvent rows never processed (no worker)
- ❌ No cache backend configured (LocMemCache default for SlotCacheService)
- ❌ No error monitoring (any prod exception = silent)
- ❌ `/api/v1/health/` returns hardcoded `{"status": "ok"}` — no real health check
- ❌ No request ID for correlation across logs

---

## PR2 — Sentry + Observability

### Scope

1. **Sentry SDK integration**
   - `sentry-sdk[django]==2.20.0` в requirements.txt
   - Init в `djangoProject/settings/base.py` (перемещаем если нужно в `prod.py` для только-prod)
   - Env vars: `SENTRY_DSN`, `SENTRY_ENVIRONMENT` (dev/staging/prod), `SENTRY_TRACES_SAMPLE_RATE` (0.1 default), `SENTRY_RELEASE` (git SHA)
   - `send_default_pii=False` (152-ФЗ защита)
   - `before_send` hook — фильтрует sensitive fields (pregnancy_status, phone нумберы если не нужны)

2. **Structured logging**
   - `python-json-logger==3.3.0` в requirements
   - `LOGGING` dict config в `settings/base.py`
     - dev.py overrides: human-readable format, DEBUG level
     - prod.py overrides: JSON format, INFO level
   - Loggers: `django`, `django.request`, `celery` (для PR3), `appointments`, `users`, `payments`, `reviews`
   - Request ID injected через middleware (см. ниже)

3. **Request ID middleware**
   - `users/middleware.py` — добавить `RequestIDMiddleware` класс
     - Генерирует UUID если нет `X-Request-ID` header
     - Кладёт в request + thread-local для logger access
     - Возвращает в response headers
   - Вклад в уже существующий middleware stack (AppTypeMiddleware, JWTContextMiddleware)

4. **Health endpoints expansion**
   - `djangoProject/urls.py` health handlers — сейчас stub
   - Создать `djangoProject/health.py`:
     - `/api/v1/health/` — liveness: database ping + cache ping (быстрый, <50ms). Returns 200 или 503.
     - `/api/v1/health/ready/` — readiness: healthy + migrations up-to-date + outbox lag check (для PR3)
   - JSON format: `{"status": "ok", "version": "1.0.0", "timestamp": "...", "checks": {"db": "ok", "cache": "ok"}}`
   - Health endpoints skip Sentry tracing (добавить в `traces_sampler`)

5. **Environment template**
   - `.env.example` — расширить новыми vars
     ```
     SENTRY_DSN=
     SENTRY_ENVIRONMENT=development
     SENTRY_TRACES_SAMPLE_RATE=0.1
     SENTRY_RELEASE=
     LOG_LEVEL=INFO
     ```
   - Comments объясняют каждую

6. **Tests**
   - `djangoProject/tests/test_health.py` — new
     - test_liveness_returns_ok
     - test_liveness_returns_503_when_db_down (mock)
     - test_readiness_returns_ok_when_migrations_applied
     - test_readiness_returns_503_when_migrations_pending
   - `users/tests/test_middleware.py` — добавить
     - test_request_id_middleware_generates_uuid_when_absent
     - test_request_id_middleware_respects_provided_id
     - test_request_id_included_in_response

7. **Docs**
   - `docs/OBSERVABILITY.md` — new
     - Sentry setup: where to get DSN, how to sign up
     - Log format reference
     - Health endpoint contracts
     - Alerting recommendations (when to page oncall)
     - Dashboard mock (что следить)

### Files touched (PR2)

```
requirements.txt                                  (modify: +2 deps)
djangoProject/settings/base.py                    (modify: +LOGGING + Sentry init)
djangoProject/settings/dev.py                    (modify: human-readable log format)
djangoProject/settings/prod.py                   (modify: JSON log format + strict Sentry)
djangoProject/urls.py                             (modify: wire health.py)
djangoProject/health.py                          (new: health check handlers)
djangoProject/tests/__init__.py                  (new)
djangoProject/tests/test_health.py              (new)
users/middleware.py                              (modify: +RequestIDMiddleware)
users/tests/test_middleware.py                   (new, if doesn't exist — check)
.env.example                                     (modify: +sentry/logging vars)
docs/OBSERVABILITY.md                            (new)
```

Total: **~12 files** (8 modifications + 4 new).

### Tests

- New test files add ~10 test cases
- Existing 412 tests must continue to pass (no regression)
- Run: `pytest --cov --cov-fail-under=82` (fail-under чуть ниже baseline 83.6% на случай мелких шумов)

### Risks (PR2)

1. **Sentry DSN not provided** — init should be no-op если `SENTRY_DSN` пустой. Test this.
2. **Log format change breaks log parsing** — no external consumers yet, low risk
3. **Health check false positives under load** — cache ping timeout must be low (100ms), fail fast
4. **Request ID conflict with existing middlewares** — middleware order matters. Place RequestIDMiddleware first.

### Rollback (PR2)

Single git revert — no migrations, no env state changes. Trivial.

---

## PR3 — Celery + Redis + Outbox Worker

### Scope

1. **Dependencies**
   - `celery[redis]==5.5.3`
   - `django-redis==5.4.0`
   - `django-celery-beat==2.7.0` (для periodic beat schedule)

2. **Celery app**
   - `djangoProject/celery.py` — new
     - `app = Celery('ayla')` (using target name, not beautygo)
     - Autodiscover tasks из installed apps
     - Task base с retry defaults
   - `djangoProject/__init__.py` — export celery app

3. **Settings**
   - `settings/base.py`:
     ```python
     CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
     CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
     CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "False") == "True"
     CELERY_TASK_EAGER_PROPAGATES = True  # fail-fast in tests
     CELERY_ACCEPT_CONTENT = ["json"]
     CELERY_TASK_SERIALIZER = "json"
     CELERY_RESULT_SERIALIZER = "json"
     CELERY_TIMEZONE = TIME_ZONE
     CELERY_BEAT_SCHEDULE = {
         "dispatch-outbox-events": {
             "task": "appointments.tasks.dispatch_outbox_events",
             "schedule": 10.0,  # every 10 seconds
         },
     }

     CACHES = {
         "default": {
             "BACKEND": "django_redis.cache.RedisCache",
             "LOCATION": os.environ.get("REDIS_URL", "redis://localhost:6379/1"),  # db 1 = cache
             "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
         }
     }
     ```
   - `django_celery_beat` добавить в `INSTALLED_APPS`
   - `settings/dev.py`: `CELERY_TASK_ALWAYS_EAGER = True` (default, override через env)
   - Tests: eager mode always on via `@pytest.fixture(autouse=True)` или test settings override

4. **Outbox dispatcher task**
   - `appointments/tasks.py` — new
     - `@shared_task(bind=True, max_retries=3, default_retry_delay=30)` decorator
     - `dispatch_outbox_events(self)`:
       - Atomic selection: `OutboxEvent.objects.select_for_update(skip_locked=True).filter(processed_at__isnull=True)[:100]`
       - Per event: lookup handler from registry, call, mark processed_at, save
       - Failure handling: increment retry_count, self.retry() if < max, else mark as failed
     - `EVENT_HANDLERS = {"booking.created": log_handler, "booking.cancelled": log_handler, ...}` (start with logging handlers; real handlers come in notifications PR)

5. **Outbox worker — infra module migration**
   - `appointments/infrastructure/outbox_worker.py` уже есть (но currently 0% coverage — not wired up)
   - **Решение:** инспектировать что там есть; либо оставить как lib для unit tests, либо удалить в пользу Celery task directly.

6. **docker-compose.yml updates**
   - Add `redis` service:
     ```yaml
     redis:
       image: redis:7-alpine
       restart: unless-stopped
       ports:
         - "127.0.0.1:6379:6379"
       volumes:
         - redis_data:/data
       healthcheck:
         test: ["CMD", "redis-cli", "ping"]
         interval: 5s
         timeout: 3s
         retries: 5
     ```
   - Add `celery_worker` service:
     ```yaml
     celery_worker:
       build: .
       command: celery -A djangoProject worker -l info --concurrency=2
       env_file: .env
       depends_on:
         redis:
           condition: service_healthy
         db:
           condition: service_healthy
     ```
   - Add `celery_beat` service (separate so only one beat scheduler runs):
     ```yaml
     celery_beat:
       build: .
       command: celery -A djangoProject beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
       env_file: .env
       depends_on:
         redis:
           condition: service_healthy
         db:
           condition: service_healthy
     ```
   - `web` сервис depends_on Redis
   - Add `redis_data` volume

7. **Environment template**
   - `.env.example`:
     ```
     REDIS_URL=redis://localhost:6379/0
     CELERY_TASK_ALWAYS_EAGER=False  # True in tests
     ```

8. **Tests**
   - `appointments/tests/test_tasks.py` — new
     - test_dispatch_outbox_events_processes_pending
     - test_dispatch_outbox_events_marks_processed_at
     - test_dispatch_outbox_events_retries_on_handler_failure
     - test_dispatch_outbox_events_skips_already_processed
     - test_dispatch_outbox_events_idempotent
     - test_handler_registry_unknown_event_type_logged_not_crashed
   - `djangoProject/tests/test_celery.py` — smoke tests
     - test_celery_app_configured
     - test_eager_mode_in_tests
   - Health endpoint update (PR2 follow-up): readiness check includes "outbox lag" — время от последнего processed event

9. **Docs update**
   - `docs/OBSERVABILITY.md` (из PR2) — добавить секцию "Celery monitoring" + outbox lag metric
   - `CLAUDE.md` update — MVP limitations section:
     - Remove "Outbox worker не запущен"
     - Remove "LocMemCache вместо Redis"
     - Add "Celery beat running every 10s for outbox dispatch"

### Files touched (PR3)

```
requirements.txt                                          (modify: +3 deps)
djangoProject/celery.py                                   (new: Celery app)
djangoProject/__init__.py                                 (modify: export celery)
djangoProject/settings/base.py                           (modify: CELERY_* + CACHES + beat schedule)
djangoProject/settings/dev.py                           (modify: eager mode default)
djangoProject/settings/prod.py                          (no changes — inherits from base)
djangoProject/tests/test_celery.py                       (new)
appointments/tasks.py                                    (new: dispatch_outbox_events)
appointments/tests/test_tasks.py                         (new: dispatcher tests)
appointments/infrastructure/outbox_worker.py             (evaluate: remove or repurpose)
docker-compose.yml                                       (modify: +redis +celery_worker +celery_beat)
djangoProject/health.py                                  (modify: +outbox lag check)
.env.example                                             (modify: +REDIS_URL +CELERY flags)
docs/OBSERVABILITY.md                                    (modify: +Celery section)
CLAUDE.md                                                (modify: remove resolved MVP limitations)
```

Total: **~14 files** (10 modifications + 4 new).

### Tests

- New test files add ~8 test cases
- Existing tests must pass with `CELERY_TASK_ALWAYS_EAGER=True`
- Coverage target: outbox_worker path coverage from 0% → ~80%+

### Risks (PR3)

1. **Redis not available in CI** — either mock or add Redis service to CI. Decision: mock in unit tests via fakeredis; integration test hits real Redis via docker-compose.
2. **Beat scheduler duplicate** — if team runs `celery beat` in multiple containers, dispatched events = duplicated. Mitigation: `django-celery-beat` uses DB row lock = only one wins.
3. **Handler failure cascade** — one bad handler crashes dispatcher, others blocked. Mitigation: try/except per-event, log failure, continue.
4. **Migration for django_celery_beat** — new migrations. Users with existing DB must run `makemigrations` → `migrate`. Instruction в PR description.
5. **Eager mode masks bugs** — tests pass but prod Celery has different semantics (transactional boundaries). Mitigation: at least one integration test with real worker.
6. **SQLite dev + django-celery-beat scheduler** — db scheduler works on SQLite, but concurrent beat locks can fail silently. Dev fallback: PersistentScheduler (не DB), или перейти на Postgres dev сразу (но это PR4 scope).

### Rollback (PR3)

Multi-step rollback:
1. `git revert` the commit
2. `docker-compose down redis celery_worker celery_beat`
3. Remove `django_celery_beat` migrations via `manage.py migrate django_celery_beat zero`

Not trivial but still reversible. Keep migrations in separate files for clean revert.

---

## Cross-cutting decisions — needs user input

### D1: Sentry account
- User has Sentry account? Y/N
- If no: we set up env to no-op (DSN empty = SDK inactive). Works.
- If yes: user provides DSN (via env, not commit). Code works either way.

### D2: Log format in dev
- Option A: JSON always (consistency)
- Option B: Human-readable dev, JSON prod (**recommended** — devs read logs локально)

### D3: Outbox dispatch frequency
- 10s (aggressive, near-realtime feel)
- 30s (less Redis/DB chatter)
- 60s (lowest cost, feels slow)
- **Recommended: 10s for now.** Outbox queue usually empty = no work. Change to event-driven trigger (signal after commit + beat as backup) later if needed.

### D4: Handler registry location
- Option A: `appointments/tasks.py` (simple, everything in one file)
- Option B: `notifications/handlers.py` (when notifications app exists — it doesn't yet)
- **Recommended: A for now.** Migrate to B when notifications app created (next PRs).

### D5: Redis dev hosting
- Option A: docker-compose (included in compose) — **recommended**
- Option B: Assume user has Redis installed locally
- **Recommended: A.** Zero-config via `docker-compose up`.

### D6: CI changes
- Check `.github/workflows/ci.yml` — does it run pytest? Does it need Redis?
- **Decision:** inspect CI first, update if needed. Probably need to add Redis service or fakeredis.

### D7: Eager mode scope
- **Tests:** always eager (deterministic)
- **Dev:** eager default for DX, override to real worker via env if testing async flow
- **Prod:** never eager — real worker + beat

### D8: Sentry PII sanitization
- Default Sentry strips known PII (auth headers, passwords)
- Additional: we add `before_send` hook filtering `pregnancy_status`, `phone_number` (если хотим быть строги) или just trust `send_default_pii=False`
- **Recommended:** start with `send_default_pii=False` + add custom filter later if Red Zone data appears в events (которого сейчас нет — memory arch deferred)

---

## Test strategy across both PRs

**Unit tests (fast, mocked):**
- PR2 health endpoints with mocked DB/cache
- PR3 dispatcher logic with `CELERY_TASK_ALWAYS_EAGER=True` + mocked handlers

**Integration tests (slower, real infra):**
- PR3 one end-to-end: create booking → OutboxEvent row → wait 15s → assert processed_at not null
- Separate test file or pytest mark `@pytest.mark.integration`
- Skipped by default, run via `pytest -m integration`

**Coverage gate:**
- After PR2: target ≥84% (up from 83.6%)
- After PR3: target ≥86% (outbox + tasks new coverage)

---

## Migration & deploy notes

### Development workflow after PR3

Before PR3: `python manage.py runserver`
After PR3: either
- Full stack: `docker-compose up` (web + db + redis + celery_worker + celery_beat + minio)
- Manual: start Redis locally + 3 processes (`runserver`, `celery -A djangoProject worker`, `celery -A djangoProject beat`)
- Minimal for backend tests: just `pytest` (eager mode, no Celery process)

### Production deploy

- CI must provision Redis (managed или self-hosted via compose)
- Deploy runs `manage.py migrate` (includes django_celery_beat)
- Two new containers: celery_worker, celery_beat
- Monitoring: Sentry alerts fire on worker failures
- Healthcheck endpoint включает outbox lag

### Rollout order (production)

1. Ship PR2 first — Sentry + logging. Low risk. Validates prod observability.
2. Monitor prod for 24h — baseline noise, tune alert thresholds.
3. Ship PR3 — Celery + Redis. Requires Redis infra provisioned, new containers up.
4. Monitor outbox lag metric — should stay <60s p95. If not, investigate handler errors в Sentry.

---

## Open questions for user

1. Есть ли у тебя Sentry account? Если да — дам инструкцию как создать project для Ayla + получить DSN.
2. Redis для prod — managed service (Yandex Cloud Redis / Selectel) или self-hosted via docker-compose? Влияет только на deployment, код использует `REDIS_URL` env.
3. CI pipeline (`.github/workflows/ci.yml`) — сейчас gonyat pytest на SQLite без Redis. Нужно добавить Redis service в CI для PR3 tests? Или eager mode покрывает?
4. PR2 и PR3 как отдельные commits + отдельные PR на GitHub, или squash в один PR "Phase 2 foundation — observability + celery"?

Ответы на 4 → приступаю к реализации в последовательности (PR2 → verify → PR3).

---

## Rough commit messages (preview)

**PR2:**
```
[feat] Phase 2 observability — Sentry + structured logging + health checks

- sentry-sdk[django] with PII scrubbing + env-controlled DSN
- python-json-logger for prod, human-readable for dev
- RequestIDMiddleware for cross-log correlation (X-Request-ID header)
- Real health endpoints (/health/ + /health/ready/) with DB/cache pings
- LOGGING config per environment

Tests: 11 new (health + middleware), 412→423 total passing.
Coverage: 83.6% → ~84.1%.

See docs/OBSERVABILITY.md for setup + alerting guide.
```

**PR3:**
```
[feat] Phase 2 activation — Celery + Redis + Outbox dispatcher

- Celery 5.5 with Redis broker + result backend
- django-redis for Django cache (SlotCacheService now hits real Redis)
- django-celery-beat for periodic tasks (DatabaseScheduler)
- Outbox dispatcher: Celery beat every 10s picks pending OutboxEvent,
  routes to handler, marks processed_at
- Handler registry stub (log-only handlers until notifications app lands)
- docker-compose: +redis, +celery_worker, +celery_beat services

Tests: 8 new (tasks + smoke), coverage outbox_worker 0%→~82%.

Closes: eng review T3, T5 (outbox activation),
partial T4 (observability for Celery).

CLAUDE.md MVP limitations updated: outbox now active,
LocMemCache replaced with Redis.
```
