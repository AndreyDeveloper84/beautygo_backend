# Pilot Smoke-Runner (W6)

Black-box smoke-прогон минимальных обязательных сценариев пилота 2026-08-15
(PILOT_CONTRACTS v1.8.0 §10) против **staging**-развёртывания обоих backend
(Ayla `beautygo_backend` + bot `ai-bot-platform`). Только HTTP (+ опциональные
read-only SQL-пробы). Ничего не деплоит, ничего не меняет в коде пакетов.

## Требования

- Python 3.12 (venv основного репо: `…/djangoproject/.venv/Scripts/python.exe`)
- `requests` (есть в venv); `psycopg2` — только для SQL-проб (`BOT_DB_DSN`)
- Сеть до staging Ayla и bot

## Переменные окружения

| Var | Назначение | Без неё |
|---|---|---|
| `AYLA_BASE_URL` | базовый URL Ayla (напр. `https://dev.gobeauty.site`) | Ayla-сценарии SKIP |
| `AYLA_INTERNAL_API_TOKEN` | internal Bearer (Ayla) | Ayla-сценарии SKIP |
| `BOT_BASE_URL` | базовый URL bot backend | bot-сценарии SKIP |
| `MAX_BOT_TOKEN` | токен MAX-бота — минт `MaxInitData` для customer API | customer/master ноги SKIP |
| `AYLA_OUTBOUND_HMAC_SECRET` (= `EVENT_INGEST_HMAC_SECRET` бота) | подпись ingest-проб | S4 SKIP |
| `BOT_DB_DSN` | `postgresql://…` read-only — SQL-пробы dedupe/reminders | SQL-проверки SKIP (SQL печатается) |
| `SMOKE_CLIENT_ID` | Ayla User UUID синтетического клиента | авто-резолв через bot export, иначе write-ноги SKIP |
| `SMOKE_SPECIALIST_ID` / `SMOKE_SERVICE_ID` | override дискавери | dynamic discovery по каталогу |
| `SMOKE_BOT_MASTER_ID` / `SMOKE_BOT_SERVICE_ID` | bot-side id для booking-ноги через miniapp API | bot booking SKIP |
| `SMOKE_TENANT_SLUG` | header `X-Tenant`, если `MULTI_TENANT_STRICT=true` | — |
| `SMOKE_TIMEOUT` | HTTP timeout, сек (default 15) | — |

## Запуск

```bash
cd djangoproject-w6   # корень worktree
PY=/c/Users/user/PycharmProjects/Ayla/djangoproject/.venv/Scripts/python.exe

export AYLA_BASE_URL=https://dev.gobeauty.site
export AYLA_INTERNAL_API_TOKEN=…
export BOT_BASE_URL=https://…
export MAX_BOT_TOKEN=…
export AYLA_OUTBOUND_HMAC_SECRET=…
export BOT_DB_DSN=postgresql://…   # опционально

$PY -m scripts.pilot_smoke.smoke --md reports/smoke-$(date +%F).md
$PY -m scripts.pilot_smoke.smoke --only S1,S5   # точечно
```

Exit code: `0` — нет FAIL; `1` — есть FAIL; `2` — нет конфигурации.
SKIP ≠ дефект: означает отсутствие входа (токен/DSN/кредо/данные) — деталь в отчёте.

## Сценарии (acceptance §10)

| # | Что проверяет | Контракт |
|---|---|---|
| S1 | Booking CRUD через internal seam: discovery specialist→service→slot; create `payment_required=false` → CONFIRMED; идемпотентный replay; create `payment_required=true` → AWAITING_PAYMENT (SKIP при 422/503); cancel → cancelled; опц. нога через bot customer API | AMD-002, §10.1–2 |
| S2 | Memory-ask: `ask-eligibility` shape → PATCH green-поля → GET содержит факт → cleanup | §10.7, PC API v1.0 |
| S3 | Billing: C2 status shape (User UUID, AMD-005); card-setup → `confirmation_url` (SKIP при 503 кредов); webhook без auth → 401/403 (AMD-014); списание+инвойс e2e — SKIP (ручной шаг, runbook §5) | C2, §10.3 |
| S4 | Eventbus: новое событие → 200 ok; replay → 200 `duplicate:true`; unknown version → 422+DLQ; unknown name → 400; SQL: dedupe-ряд один, `booking.created` доехал (D-3 flip) | C4, AMD-007/008 |
| S5 | C5 dual-system: факт в Ayla export → bot delete каскад → память пуста и в Ayla (export → null); идемпотентность повтора; bot export — `Content-Disposition: attachment` | C5, AMD-006/010 |
| S6 | R1: по `ayla_appointment_id` ровно `day_before` + `two_hours`, без дублей; SQL-нога | R1, AMD-012 |
| S7 | UX-пробы: export blob-download контракт (MAX webview); stub-gate — wellness/recent-activity/recommendations без stub-маркеров | W4 бриф |

## Гигиена staging

- Синтетические id (`bot:max:900000001`, префиксы `smoke-`), созданные брони отменяются.
- S2 восстанавливает исходное значение поля; если факта не было — C5 wipe синтетического user.
- SQL-пробы — только SELECT (dedupe/DLQ/reminders).

## Известные preflight-зависимости (см. runbook §4)

1. `EVENT_INGEST_HMAC_SECRET` должен реально попадать в settings бота (config-gap:
   из env не загружается) — иначе все ingest-пробы дают 401 `no_secret`.
2. `OUTBOX_EXTERNAL_DELIVERY_TOPICS` (Ayla) пуст по умолчанию — round-trip booking.*
   в бота требует flip (решение оркестратора, D-3).
3. ЮKassa: test-shop creds на staging, иначе S1(`payment_required=true`)/S3(card-setup) → 503 SKIP.
4. SQL-таблицы бота: `eventbus_ingestdedupe` (колонка `received_at`, НЕ `first_seen_at`
   из устаревшего рецепта), `eventbus_ingestdlq`, `booking_bookingreminder`.

## Отчёт

Stdout — таблица `сценарий → PASS/FAIL/SKIP → деталь`; `--md` — полный markdown
(та же таблица + все проверки). Формат соответствует брифу W6: «сценарий → PASS/FAIL → деталь».
