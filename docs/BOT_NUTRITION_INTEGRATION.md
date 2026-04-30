# MAX Bot ↔ Ayla Nutrition Integration

> Status: SHIPPING (DRF-246 done 2026-04-30) · Owner: Andrey · Last updated: 2026-04-30
> Tickets: DRF-246 (scan), DRF-247 (food log + summary), DRF-248 (deficit→service bridge)

## Why this exists

The MAX bot for «Формула тела» runs on production (30+ days, ~Penza beauty
audience) and that audience is functionally the Ayla ICP. Validating
hypotheses on a separate `@AylaTestBot` would mean recruiting 20 strangers
and waiting 14 days. Instead, we surface live Ayla features inside the
existing bot and measure live behavior — telemetry already lands in
`Message.action_type` / `tokens_in,out` / `latency_ms`.

H1 (food scanner is a daily-hook), H3 (cross-domain bridge from food
deficit → beauty service is what makes Ayla feel different from
MyFitnessPal+Yclients) and H5-adjacent retention questions all become
testable without mocks once the bot can talk to Ayla's nutrition API.

## Problem: bot ≠ Ayla user

The bot stores `BotUser` in mysite's Django (`services_app.BotUser`), keyed
by `max_user_id: BigInteger`. Ayla `User` is a UUID-pk AbstractUser in a
separate Django process and DB. There is no shared identity yet — Phase C
will introduce that. We need scanner working **today** to validate H1.

## Solution: service-to-service auth + lazy ProxyUser

```
┌──────────────── mysite/maxbot ────────────────┐    ┌───── Ayla djangoproject ─────┐
│ handlers/food_scanner.py                       │    │ POST /api/v1/nutrition/      │
│   on photo →                                   │    │      internal/scan/          │
│     resolve_bot_user                           │    │ ──────────────────────────── │
│     gate: BotUser.food_scanner_consent_at      │    │ permission_classes:          │
│     download photo (MAX CDN, 10 MiB cap)       │    │   [IsServiceAccount]         │
│     external_user_id_for(bot_user) →           │    │                              │
│       "bot:{max_user_id}"                      │    │ Headers (required):          │
│              │                                 │    │   X-Service-Token: <secret>  │
│              ▼                                 │────│→  X-External-User-ID:        │
│ services/nutrition_client.py                   │    │     "bot:{max_id}"           │
│   NutritionClient(httpx async)                 │    │                              │
│   - timeout 10s                                │    │ resolve_external_user(...)   │
│   - circuit breaker (3 fail/60s → 60s skip)    │    │   → User(is_proxy=True,      │
│              │                                 │    │           role='client')     │
│              ▼                                 │    │   (lazy get_or_create)       │
│ ai_ui.py::render_food_scan(scan_dict)          │    │                              │
│   → MAX inline keyboard (4 meal-type buttons)  │    │ FoodScan saved against the   │
└────────────────────────────────────────────────┘    │ proxy User. S3 photo TTL=30d │
                                                      └──────────────────────────────┘
```

## Auth model

| Layer | What | Why |
|---|---|---|
| `IsServiceAccount` | `compare_digest(request.X-Service-Token, settings.NUTRITION_SERVICE_TOKEN)`; fail-closed on empty token | Constant-time comparison; misconfigured deploys reject everything rather than accept everything |
| `X-External-User-ID` regex `^[a-z][a-z0-9_-]*:[A-Za-z0-9_-]{1,64}$` | Validated in `users.services.resolve_external_user` | Forces a typed namespace (`bot:`, future `formula:`, `web:`) — accidental collisions with real usernames are impossible by construction |
| `User.is_proxy=True` | Boolean flag, audit | Admin/analytics can filter proxy from real users. Phase C migration links proxy→real via a new `User.linked_proxy_id` (not yet built) |

The bot **does not** carry a JWT — `IsServiceAccount` is the only check.
`IsClient`/`IsClientApp` would not work: bot users are not real Ayla
clients yet, and `IsClient` enforces `not is_guest`.

## Wire format

### Request

```http
POST /api/v1/nutrition/internal/scan/ HTTP/1.1
Host: ayla.example.com
X-Service-Token: <NUTRITION_SERVICE_TOKEN>
X-External-User-ID: bot:12345
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="image"; filename="meal.jpg"
Content-Type: image/jpeg

<bytes, ≤10 MiB>
--...
Content-Disposition: form-data; name="portion_multiplier"

1.0
--...--
```

### Responses

| HTTP | `error.code` | Meaning | Bot action |
|---|---|---|---|
| 200 | — | `data.{id, dish_name, confidence, portion_g, nutrition, provider}` | Render card + diary buttons |
| 400 | `VALIDATION_ERROR` | Bad image / bad external_user_id | Plain text «попробуй ещё раз» |
| 400 | `FOOD_NOT_RECOGNIZED` | Vendors all returned low-confidence | Plain text «не получилось распознать» |
| 403 | — | Service token missing/wrong | Misconfig — alert ops, do NOT retry |
| 503 | `FOOD_API_UNAVAILABLE` | All vendors down/timeout | Plain text «временно недоступно» + circuit breaker |

## Resilience

- **Bot circuit breaker** (`maxbot.services.nutrition_client._Circuit`):
  3 failures within 60 s → next calls short-circuit for 60 s. Per-worker
  state (no shared cache); if Ayla is hard-down, every worker opens its
  breaker independently within seconds.
- **MAX CDN race**: photo URLs are short-lived; `food_scanner.py` streams
  the bytes immediately into memory rather than handing the URL to Ayla.
- **Idempotency**: not needed for `/scan/` — it's strictly create-only.
  `/food-log/` (DRF-247) will use `X-Idempotency-Key`.

## 152-ФЗ consent

The bot blocks the first scan until `BotUser.food_scanner_consent_at` is
set. Consent is gathered with two callback buttons:

```
cb:nutrition:consent:agree   → set timestamp, "пришли фото снова"
cb:nutrition:consent:decline → no-op, "сканер доступен когда передумаете"
```

Photo is sent to OpenAI Vision (US) or Yandex Vision (RU) by Ayla's
`FoodScannerRouter`. Bucket lifecycle on S3 enforces 30-day TTL — bot
side does not need to track it.

## Settings (both sides must be set in lockstep)

| Key | Where | Value |
|---|---|---|
| `NUTRITION_SERVICE_TOKEN` | Ayla `djangoProject/settings/base.py` + mysite `mysite/settings/base.py` | `openssl rand -hex 32`, identical in both env files |
| `AYLA_BASE_URL` | mysite only | `http://localhost:8001` (dev) / `https://api.ayla.app` (prod) |
| `FOOD_SCANNER_PRIMARY` / `FOOD_SCANNER_FALLBACK` | Ayla only | `openai` / `yandex` (already configured for `/scan/` client endpoint) |

Token rotation: quarterly. Coordinated deploy required — switching one
side first will 403 every bot scan until the other catches up.

## Test matrix

### Ayla side (`nutrition/tests/test_internal_food_scan.py`)

- Auth boundary: missing token, wrong token, JWT user (must NOT bypass), empty settings token (fail closed)
- Resolution: first call creates ProxyUser, second call reuses, invalid ID format → 400, missing header → 400
- Happy path: scan persists against ProxyUser with `is_proxy=True`

### Bot side

- `tests/maxbot/test_nutrition_client.py` (7 cases): 200 / 400 FOOD_NOT_RECOGNIZED / 503 unavailable / 5xx → circuit opens after 3 fails / unknown 4xx / settings validation / headers carry token+external_id
- `tests/maxbot/test_ayla_user_proxy.py` (3 cases): format, idempotence, BigInteger fits

## Operational runbook

**Disable the integration in prod (e.g. Ayla incident):**
1. Set `NUTRITION_SERVICE_TOKEN=""` in mysite env → `IsServiceAccount` will fail → all scans get 403 → bot circuit opens fast → users see «временно недоступно». Restart mysite worker.
2. Or simpler: set `AYLA_BASE_URL=""` in mysite → `get_nutrition_client()` raises on first use → handler hits `NutritionAPIError` branch.

**Rotate token:**
1. Generate new token.
2. Set `NUTRITION_SERVICE_TOKEN=<new>` in Ayla env → restart Ayla.
3. Set `NUTRITION_SERVICE_TOKEN=<new>` in mysite env → restart mysite.
4. Step 2 must happen before step 3 — gap is bot-broken-but-Ayla-fine.
   For zero downtime, accept TWO tokens server-side temporarily (not yet implemented; 60-second outage during rotation is acceptable).

**Migrate proxy → real account (Phase C, future):**
1. User registers in Ayla mobile app → obtains real `User` UUID.
2. Bot prompts user «привязать к Ayla?»; on confirm, mysite calls a new
   Ayla `/api/v1/auth/link-proxy/` endpoint with both real-account JWT
   and `bot:{max_user_id}`.
3. Ayla sets `real_user.linked_proxy_id = proxy_user.id`. All future
   scans use the real `external_user_id` (real UUID) — proxy stays as
   audit history.

## Open follow-ups

- [ ] DRF-247: `InternalFoodLogView` + `InternalSummaryView` mirror endpoints. Bot `/дневник` command + meal-type buttons on scan card.
- [ ] DRF-248: `InternalDeficitsView` + `extra_hint` kwarg in `ayla_ai_core.render_system_prompt`. Killer-scenario S2→S4 bridge.
- [ ] Token rotation needs zero-downtime path (accept 2 tokens in `IsServiceAccount`).
- [ ] Sentry tag `service=bot-formula` on internal endpoints for cost attribution.
- [ ] Phase C: `User.linked_proxy_id` + `/auth/link-proxy/` endpoint.

## References

- `nutrition/views.py:158` — `InternalFoodScanView`
- `users/permissions.py:59` — `IsServiceAccount`
- `users/services.py:16` — `resolve_external_user`
- `mysite/maxbot/services/nutrition_client.py` — bot HTTP client
- `mysite/maxbot/handlers/food_scanner.py` — MAX webhook + consent flow
- `docs/FOOD_SCANNER_DECISION.md` — Plan Y+ multi-vendor architecture
- Linear: DRF-246 (this doc), DRF-247, DRF-248
