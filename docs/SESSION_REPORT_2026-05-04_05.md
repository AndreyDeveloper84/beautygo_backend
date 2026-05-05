# Session Report — Ayla Backend & ayla-ai-core Shared Package

> **Период:** 2026-05-03 → 2026-05-05
> **Скоуп:** AI Chat infrastructure (ayla-ai-core wire-up), multi-tenant strict-flag rollout, UserPersonalContext в prompt, Analytics ingestion endpoint, H3 hypothesis validation pack, post-deploy fixes
> **Все merged на `dev` (репо `AndreyDeveloper84/beautygo_backend`) + один merged PR в `AndreyDeveloper84/ayla-ai-core`**

---

## TL;DR

12 закрытых PR в backend-репо + 1 в shared package за сессию. Ключевые выходы:

1. **AI Chat теперь работает через shared package** `ayla-ai-core` v0.6.0 (вместо локальных дубликатов tools.py/handlers/prompts) — single source of truth для orchestration, разные wire-format'ы у Ayla и бота сосуществуют через DI hook.
2. **Multi-tenant strict mode включён на dev** через env-gated flag — `MULTI_TENANT_STRICT=True` в dev settings, прод остаётся opt-in.
3. **UserPersonalContext подключён в LLM system prompt** — закрыт «AI который помнит» promise для explicit signals (mobile PATCHит preferences → LLM их видит).
4. **Analytics ingestion endpoint** `POST /api/v1/analytics/event/` — последний M3 P0 untouched item; mobile может слать события для North Star метрик.
5. **DRF-232 H3 SQL pack** для Decision Day 2026-05-13 — read-only run-book для верификации memory-hypothesis на production бот-данных.
6. **5 post-deploy фиксов** для smoke / CI / lint.

Всего: **+~3500 строк кода + docs**, **+~600 строк тестов** (43 новых теста + 5 новых тестов в shared), **0 регрессий** на финале.

---

## 1. Карта PR'ов (хронологически)

| # | Title | Тикет | Merged | Lines | Tests |
|---|-------|-------|--------|-------|-------|
| **#43** | AIConcierge contract hardening | DRF-240 | 2026-05-04 | +162/-6 | +8 |
| **#56** | strict X-Tenant enforcement (env flag) | DRF-242.5 | 2026-05-04 | +326/-20 | +6 |
| **#57** | wire ayla-ai-core (Slice A) | DRF-241 | 2026-05-04 | +591/-8 | +21 |
| **#79** | chat_service drives AIConcierge (Slice B) | DRF-241 | 2026-05-05 | +268/-308 | (regression) |
| **#85** | UserPersonalContext into AI Chat prompt | DRF-230 wire-up | 2026-05-05 | +399/-0 | +22 |
| **#87** | flip strict-flag on dev settings | DRF-242.5 | 2026-05-05 | +13/-0 | (smoke 84/84) |
| **#90** | H3 memory hypothesis SQL pack | DRF-232 | 2026-05-05 | +456/-0 | (docs only) |
| **#91** | Analytics event ingestion endpoint | DRF-160 | 2026-05-05 | +581/-0 | +14 |
| **#94** | flake8 E501 on analytics throttle comment | DRF-160 fix | 2026-05-05 | +4/-1 | — |
| **#95** | strict-mode + analytics fallout | post-deploy fix | 2026-05-05 | +18/-1 | — |
| **#96** | install ayla-ai-core into running web container | smoke fix | 2026-05-05 | +29/-1 | — |
| **#97** | install git for pip git+https | smoke fix | 2026-05-05 | +5/-0 | — |
| **shared #5** | tool_dispatcher DI hook + 0.6.0 | DRF-241 prereq | 2026-05-05 | +191/-9 | +4 |

---

## 2. Хронология и зависимости

```
DRF-240 (PR #43)        ← модельные дельты (uniqueness, soft-delete, action-type index)
   │
   └─ enables ───────► DRF-241 Slice A (PR #57)         ← stores.py + concierge_factory.py
                          │
                          └─ requires ────► ayla-ai-core 0.6.0 (shared #5) ← tool_dispatcher hook
                                              │
                                              └─ enables ──► DRF-241 Slice B (PR #79)  ← chat_service на AIConcierge
                                                                │
                                                                └─ enables ──► DRF-230 wire-up (PR #85) ← personal-context block в prompt

DRF-242.5 strict (PR #56) ← env-gated implementation
   │
   └─ enables ───────► DRF-242.5 dev rollout (PR #87)   ← flip MULTI_TENANT_STRICT=True в dev.py
                          │
                          └─ broke 354 unit tests + smoke ─► PR #95 (test settings override + analytics-cp в smoke)

DRF-160 Analytics (PR #91)
   │
   └─ broke flake8 ─────► PR #94 (E501 на comment)
   │
   └─ broke smoke (analytics не в cp list) ─► PR #95 (см. выше — два бага одним фиксом)

PR #95 unblocked smoke far enough to ImportError on `ayla_ai_core` (VPS image предшествует #57)
   │
   └─► PR #96 (pip install в running container с GH_DEPLOY_TOKEN)
       │
       └─ pip install fail на missing git ─► PR #97 (apt-get install git just-in-time)

DRF-232 SQL pack (PR #90) — read-only docs, без code зависимостей
```

---

## 3. Подробно по тикетам

### 3.1 DRF-240 — AIConcierge contract hardening (PR #43)

**Цель:** Pre-DRF-241 модельные дельты, чтобы `ConversationStore` Protocol из ayla-ai-core чисто маппился на Django ORM.

**Что меняется:**

- **Partial unique constraint** `UNIQUE(user, tenant_id) WHERE is_active=true AND deleted_at IS NULL` на `ai.Conversation` — `resolve_active_conversation()` обязан возвращать ровно одну active per (user, tenant). Без констрейнта параллельные `POST /ai/chat/` от одного клиента создают дубли.
- **SoftDeleteManager** как `objects` (default) + `all_objects` для админки + `Conversation.mark_deleted()` метод — фундамент под 152-ФЗ «Очистить историю Ayla» и просто гигиена queryset-ов. Админка переключается на `all_objects` чтобы видеть удалённые для аудита.
- **Composite index `(role, action_type)`** на `ai.Message` — analytics-нагрузка из `BOT_CODE_AUDIT_2026-04` §1.6 (`SELECT action_type, COUNT(*) FROM messages WHERE role='assistant' GROUP BY action_type` на каждом ops-дашборде) была seq-scan-ом.

**Тесты:** 8 новых (Manager filtering, mark_deleted, partial-unique constraint, inactive не блокирует, soft-deleted не блокирует, role+action_type индекс), фикс существующего `test_filter_by_tenant_id` под новый инвариант.

**Файлы:** `ai/models.py`, `ai/admin.py`, `ai/tests/test_models.py`, `ai/migrations/0003_one_active_conversation_per_user_plus_action_index.py`.

---

### 3.2 DRF-242.5 — strict X-Tenant enforcement (PR #56)

**Цель:** Env-gated `MULTI_TENANT_STRICT` toggle в `TenantContextMiddleware`.

**Поведение:**

- **`MULTI_TENANT_STRICT=true`:** `/api/v1/*` без валидного `X-Tenant` → **400 `TENANT_REQUIRED`** на уровне middleware (до auth, до permissions). Отличает «ты забыл header» (400) от «ты не в этом tenant» (403).
- **Opt-out paths:** `/api/v1/auth/*` (registration handshake — pre-tenant), `/api/v1/health/`, `/api/v1/nutrition/internal/*` (bot service-to-service in `EXCLUDED_PATH_PREFIXES`).
- **`MULTI_TENANT_STRICT=false`** (default): идентичное поведение DRF-242.4 — нулевая регрессия.

**Rollout sequence (см. `docs/MULTI_TENANT.md`):**

1. `manage.py backfill_tenants` на prod (idempotent)
2. Verify `User.objects.filter(tenant__isnull=True, is_active=True).count() == 0`
3. `MULTI_TENANT_STRICT=true` env на dev → smoke
4. То же на staging → 24h soak
5. То же на prod

**Rollback:** `MULTI_TENANT_STRICT=false`, restart воркеров. Никакой миграции откатывать не нужно — схема идентична permissive режиму.

**Тесты:** 6 новых strict-mode тестов (missing header 400, unknown slug 400, known slug pass, excluded path bypass, auth opt-out pass, STRICT=false regression). 18/18 tenants tests зелёные.

---

### 3.3 ayla-ai-core 0.6.0 — tool_dispatcher DI hook (shared #5)

**Контекст:** ayla-ai-core 0.5.x bundled `dispatch_tool_call` использовал бот-Формула naming (`show_masters`, `master_id`). Ayla API spec v2.0 фиксировала `show_specialists`, `specialist_id`. Бот в проде 30+ дней эмиттит `show_masters`, бот ушёл в свой Phase 2.4 с локальным `recommend_services` tool которого нет в shared.

**Решение — Variant (d):** shared package становится infrastructure-only. Каждый consumer держит свои `tools.py` + `tool_handlers.py` (Ayla wire-format в Notion spec, бот wire-format в LLM trained behavior). Shared package добавляет DI-hook `tool_dispatcher: Callable[[tool_call, context], ToolResult]` на `AIConcierge.__init__`.

**API:**

```python
AIConcierge(
    openai_client=...,
    store=...,
    context_builder=...,
    tool_dispatcher=ayla_local_dispatch,  # NEW, optional
)
```

**Backward compat:** `tool_dispatcher=None` (default) → bundled `dispatch_tool_call` как раньше. Каждый existing consumer keeps current behaviour без code change.

**Тесты:** 4 новых в `test_orchestrator.py` (dispatcher invocation, default fallback when None, raw tool_call shape passed through, id_parser skipped under custom dispatch). 119/119 shared tests зелёные.

**Версия:** 0.5.0 → **0.6.0** (minor — additive, fully backward compatible). Tag `v0.6.0` создан на main.

---

### 3.4 DRF-241 Slice A — wire ayla-ai-core (PR #57)

**Цель:** Infrastructure plumbing для swap локального AI Chat pipeline на ayla-ai-core. **Никакого изменения поведения** в этом PR — `chat_service` продолжает гнать локальный pipeline. Этот слайс закладывает seam.

**Что добавлено:**

| Файл | Назначение |
|------|-----------|
| `ai/stores.py` | `DjangoConversationStore` — sync ORM-адаптер для `ayla_ai_core.orchestrator.ConversationStore` Protocol. Race-safe через partial-unique constraint из DRF-240. Читает `user.tenant` (DRF-242.3 FK) для multi-tenant scoping. |
| `ai/concierge_factory.py` | `get_concierge_for(actor)` — DI-builder. `tool_definitions=build_tool_definitions("string")` (UUID wire-format), `id_parser=_safe_uuid`. Включает translator local `SpecialistContext` → ayla-ai-core `SpecialistContext[UUID]`. |
| `requirements.txt` | Раскомментирован `ayla-ai-core` через git+https URL (`@v0.5.1`). Документированы 2 варианта CI auth. |

**Тесты:** 21 новый — 14 в `test_stores.py` (Protocol conformance, idempotency, soft-delete, tenant FK reading, save behaviour, history ordering / exclude / limit), 7 в `test_concierge_factory.py` (local→core context translation, marketplace voice, DRF-248 extra_hint hand-off, AIConcierge type, UUID wire-format).

---

### 3.5 DRF-241 Slice B — chat_service drives AIConcierge (PR #79)

**Цель:** Заменить локальный LLM pipeline в `chat_service` одним вызовом `AIConcierge.send_message()` из ayla-ai-core 0.6.0.

**Архитектура:**

```
ChatService.send_message(actor, conversation_id, message_text, ctx)
  ├── 1. check_anonymous_limit()        → 429 RATE_LIMITED (anon_message_limit)
  ├── 2. check_daily_token_limit()      → 429 RATE_LIMITED (daily_token_limit)
  ├── 3. redact_pii(message_text)       → redacted_text
  ├── 4. concierge.send_message(        ← AIConcierge из ayla-ai-core делает всё остальное:
  │       user_key=actor,                │   resolve_conversation → save_user_msg →
  │       message_text=redacted_text,    │   build_context → load_history →
  │       prompt_renderer=...            │   render_prompt → call_openai →
  │     )                                │   dispatch_tool_call (Ayla local) →
  │                                      │   save_assistant_msg
  ├── 5. update_token_counter()         → Redis post-call
  └── 6. return ChatResponseDTO
```

**Per-request closure pattern в `concierge_factory.get_concierge_for(actor)`:**

- `context_builder` пишет local rich `SpecialistContext` (со score / distance / match_reasons) в per-request dict
- `tool_dispatcher` читает оттуда. AIConcierge зовёт builder перед dispatcher внутри `send_message()`, поэтому read всегда populated
- Dispatcher closes over `actor` чтобы протащить `client_id` в local handlers — anonymous users всё равно получают `show_appointments` clarification fallback

**Wire-format strategy (Variant d):** Ayla оставляет свой `ai/tools.py` + `ai/tools_handlers.py` + `ai/prompts.py`. ayla-ai-core 0.6.0 `tool_dispatcher` hook позволяет local dispatcher работать внутри AIConcierge без форка orchestrator.

**Privacy semantic change:** PII redaction теперь применяется **до persistence** — Message row хранит redacted text, не raw. Тот же redacted text уходит в LLM. Audit-friendly «raw в DB, redacted в LLM» контракт потребует `pii_redactor` hook в AIConcierge — tracked как follow-up shared package bump (0.7.0).

**Тесты:** 16/16 chat_service зелёные после refactor (PII test переписан под новую policy), 9/9 concierge_factory зелёные (3 assertion обновлены под новый контракт), conftest patch_openai теперь использует `AsyncMock`. 151/151 ai/ зелёные локально.

---

### 3.6 DRF-230 wire-up — UserPersonalContext в LLM prompt (PR #85)

**Контекст:** Mobile PATCH-ит `/users/me/personal-context/` с явными preferences (preferred_districts, preferred_time_slots, price_range_min/max, diet_type, skin_sensitivities, prefers_flexible_cancellation) с момента DRF-174 (PR #37). До этого PR'a AI Chat **не видел ничего из этого** — LLM каждый раз спрашивал заново те же вопросы, ломая «AI который помнит» promise.

**Реализация:** Подключает сохранённый контекст в system prompt через `render_system_prompt(extra_hint=...)` slot из ayla-ai-core 0.6.0 — тот же механизм что DRF-248 использует для bot nutrition deficits.

**Пример вывода:** при `preferred_districts=["Тверская"], preferred_time_slots=["evening"], price_range_max=5000, diet_type="vegetarian"`:

```
ИЗВЕСТНЫЕ ПРЕДПОЧТЕНИЯ КЛИЕНТА:
- предпочитаемые районы: Тверская
- удобное время: вечер (17–21)
- бюджет: до 5000 ₽
- диета: вегетарианство
```

Wrapped ayla-ai-core'овским advisory wrapper'ом «ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ (мягкая подсказка, не правило)» — LLM трактует как hint.

**Lazy lookup pattern:**

- Guest users скипают целиком (row keyed на real User)
- Authenticated users без row возвращают `""` (lazy creation: row появляется на первый PATCH от mobile, не через signal)
- `ObjectDoesNotExist` catch обрабатывает not-yet-PATCHed case чисто

**Out of scope (Phase 6 per CEO scope reduction):**

- Behavioural inference Celery (паттерны → fields)
- LLM extraction из chat free-text
- Three sensitivity zones (green/yellow/red)
- Anti-spam cooldowns (8 правил)
- 152-ФЗ DELETE field / total wipe endpoints
- Encryption at rest

**Тесты:** 22 новых (19 в `test_personal_context_hint.py` — pure-function: empty, каждое поле в изоляции с capping, composition, unknown-enum fallbacks, money formatting; 3 в `test_chat_service.TestPersonalContextInjection` — end-to-end через ChatService: no row → no block, populated row → все поля видны в LLM-bound prompt, guest skip).

---

### 3.7 DRF-242.5 dev rollout — flip strict-flag (PR #87)

**Цель:** Step 1/3 strict-mode rollout per `docs/MULTI_TENANT.md` §Rollout: dev → staging → prod.

**Реализация:**

```python
# djangoProject/settings/dev.py
if "MULTI_TENANT_STRICT" not in os.environ:
    MULTI_TENANT_STRICT = True
```

State version-controlled (rollback = revert), env-var override доступен для emergency rollback без re-deploy. Production остаётся opt-in через env var (`prod.py` наследует от `base.py`, никакого override).

**Verified:** Smoke 84/84 локально под strict mode (internal-paths не трогаются — в `EXCLUDED_PATH_PREFIXES`).

**⚠️ Регрессия (потом fixed в #95):** test.py наследует от dev.py → MULTI_TENANT_STRICT=True transitively применилось к unit-тестам, 354 теста сломались на CI. Lesson learned ниже в Section 5.

---

### 3.8 DRF-232 H3 SQL pack (PR #90)

**Цель:** Read-only run-book для Decision Day **2026-05-13** — 6 секций PostgreSQL-запросов против production бота (`mysite/`) для go/no-go по UserPersonalContext полной реализации (Phase 6).

**Файл:** `docs/DRF_232_H3_SQL_PACK.md` — 456 строк.

**Секции:**

| # | Question | Decisive metric |
|---|----------|----------------|
| 1 | Где AI тратит turns? | `action_type` distribution |
| 2 | Funnel `show_masters → confirm_booking` | `pct_masters_to_confirm` |
| 3 | Returning bookers loyal to one master? | `loyalty_pct` |
| 4 | LLM grounding health | `pct_pure_text` |
| 5 | Memory-relevant signals (categories, time-of-day) | category/hour distribution |
| 6 | Token spend trajectory | $/day |

**Decision matrix:**

| Signal | Verdict |
|--------|---------|
| `pct_masters_to_confirm` ≥ 25% AND `loyalty_pct` ≥ 30% AND `pct_pure_text` < 30% | **Build full UserPersonalContext** (Phase 6 deferral revisited) |
| Borderline на одной метрике | **Ship DRF-230 wire-up only** (уже сделано в PR #85), A/B post-pilot |
| `pct_pure_text` ≥ 30% OR `pct_masters_to_confirm` < 10% | **Fix LLM grounding first**, не строить memory |

SQL валидирован против `mysite/services_app/models.py` schema (Conversation `:1609`, Message `:1738`, BookingRequest `:1258`, BotUser `:1099`). Все запросы используют существующие индексы.

**Decision thresholds выровнены** с `docs/HYPOTHESIS_VALIDATION_PLAN_2026-04.md` H3.

---

### 3.9 DRF-160 — Analytics ingestion endpoint (PR #91)

**Цель:** `POST /api/v1/analytics/event/` — единый durable intake для mobile-emitted telemetry. Закрывает **последний M3 P0 untouched** — без него у pilot нет инструментирования для North Star (context fill rate, AI funnel conversion lift, drop-off shape).

**Дизайн:**

| Решение | Почему |
|---------|--------|
| Generic event row + freeform `payload` JSON | Schema-on-read в BI. Новые события — одна строка в catalogue, без миграций |
| Code-side whitelist (`analytics/event_catalogue.py`, **31 событие**) | Single source of truth. Drift ловится 400 `UNKNOWN_EVENT_NAME` сразу |
| Idempotency на `(actor, client_event_id)` или `(anonymous_session_id, client_event_id)` для guests | Retry-safe, второй POST → 200 с тем же id |
| `IsAuthenticated`, без `IsClientApp` гейта | Обе апликации emit-ят телеметрию, `app_type` из header'а пишется в row, cohorts split в BI |
| Tenant denormalised from `request.tenant` | Будущий per-tenant dashboard scope |
| Scoped throttle `analytics_event` = 300/min | Mobile может batch-emit на session foreground/background |

**Whitelisted events (31):**

| Phase | События |
|-------|---------|
| Booking | `booking_viewed/created/cancelled/rescheduled/completed` |
| AI Chat | `ai_chat_opened/message_sent`, `ai_action_shown/confirmed/rejected`, `ai_clarification_answered` |
| Nutrition | `food_scan_taken/confirmed`, `food_log_added_manual`, `water_logged`, `daily_summary_viewed` |
| Personal Context (DRF-174/230) | `personal_context_field_set/cleared`, `context_question_skipped` |
| Lifecycle | `app_opened`, `onboarding_started/completed`, `push_received/tapped` |
| Pro app | `pro_dashboard_viewed`, `pro_booking_viewed/actioned` |
| Search | `search_performed`, `specialist_viewed/favorited/unfavorited` |

**Тесты:** 14 новых — auth (401/anon/real), validation (UNKNOWN_EVENT_NAME branch + VALIDATION_ERROR fallback + payload-type coercion), idempotency (per-actor + per-anonymous-session), provenance (app_type/tenant/payload/client_timestamp), catalogue smoke. **229/229** analytics + ai + tenants tests green вместе.

**Файлы:** `analytics/{__init__,apps,event_catalogue,models,migrations/0001_initial,serializers,views,urls,tests/test_event_endpoint}.py`, `core/errors.py` (UNKNOWN_EVENT_NAME), `djangoProject/settings/base.py` (INSTALLED_APPS + analytics_event throttle), `djangoProject/urls.py` (mount).

---

## 4. Post-deploy фиксы (хронология ошибок)

### 4.1 PR #94 — flake8 E501 на analytics_event throttle comment

**Trigger:** PR #91 merged → CI/CD упал на flake8.

```
./djangoProject/settings/base.py:103:121: E501 line too long (209 > 120 characters)
```

**Fix:** Wrapped inline comment в 3-line block над dict entry. Same content, no behaviour change.

**Lesson:** Локально flake8 на правленных файлах не запустил. Должен делать перед push.

---

### 4.2 PR #95 — strict-mode + analytics smoke fallout

**Триггер 1:** PR #87 merged → CI/CD pytest упал на 354 failure.

```
test.py inherits from dev.py
PR #87 set MULTI_TENANT_STRICT=True в dev.py для VPS rollout
test.py подхватил это transitively
APIClient в тестах не шлёт X-Tenant header
→ middleware 400'ит каждый /api/v1/* call до view
→ 354 теста fail
```

**Триггер 2:** PR #91 merged → smoke на dev VPS упал на:

```
ModuleNotFoundError: No module named 'analytics'
```

VPS web image не пересобирается на каждый merge (private-dep auth gap, см. workflow header). Smoke workflow хирургически `docker compose cp`-ит фиксированный список директорий (`nutrition`, `djangoProject`, `ai`, `tenants`) в running container. `analytics/` не был в этом списке → Django startup ImportError'ил до того как любой тест запустился.

**Fix:**

1. `MULTI_TENANT_STRICT=False` в `test.py` (explicit override)
2. Добавил `analytics` в smoke workflow find-prune list + cp block + explicit `mkdir -p /app/analytics`

**Verification:**

- 23/23 `users/tests/test_specialists_api.py` зелёные локально (representative failing slice)
- 18/18 `tenants/tests/test_middleware_and_permission.py` зелёные (strict-mode тесты flipают флаг per-test через fixture)

**Lesson:** Перед merge'ем PR который меняет `dev.py` settings — проверять что test.py не наследует поведение, или явно override-ить в test.py.

---

### 4.3 PR #96 — install ayla-ai-core в running container

**Триггер:** PR #95 unblocked Django startup, но 69/84 nutrition smoke тестов упали:

```
ModuleNotFoundError: No module named 'ayla_ai_core'
```

VPS web image предшествует DRF-241 Slice A merge (PR #57). PR #57 добавил `ayla-ai-core @ git+https://...@v0.5.1` в requirements.txt. Slice B (PR #79) сделал `ai/views.py` импортировать `from ai.concierge_factory import ...` который импортирует `ayla_ai_core`. PR #91 (analytics) только дополнил.

VPS image НЕ пересобирался — workflow header прямо документирует это:

> "The Dockerfile runs pip install -r requirements.txt, which clones the private ayla-ai-core dependency from GitHub. The CI runner has GH_DEPLOY_TOKEN configured for this; the VPS docker build context does NOT have access to it, so a --no-cache rebuild on the box fails at the pip install step."

Так что `docker compose cp ./ai/.` копирует новые `ai/views.py` (которые импортируют `ayla_ai_core`) в running container где этого пакета нет.

**Fix:** установить пакет в running container через `docker compose exec -T web pip install ...`, прокинув `GH_DEPLOY_TOKEN` через `envs` whitelist SSH action'а. Pin `@v0.6.0`. Идемпотентно. Fail-loud если token отсутствует. Import-probe после установки.

**Lesson:** При появлении нового runtime-dependency в shared package — обновить smoke workflow (или fix the underlying VPS image rebuild gap).

---

### 4.4 PR #97 — install git для pip git+https

**Триггер:** PR #96 retry упал на:

```
ERROR: Error [Errno 2] No such file or directory: 'git' while executing command git version
```

`python:3.12-slim` base image не несёт git, а `pip install ayla-ai-core @ git+https://...` shells out to git для clone.

**Fix:** apt-get install git just-in-time в running container, guarded by `command -v git` (idempotent на re-run).

**Финальный результат smoke:** **84/84 nutrition tests pass** на dev VPS.

**Lesson:** При plumbing новых dependencies — учитывать что slim base images минималистичны. Альтернатива — pre-built wheel вместо git+https.

---

## 5. Architectural decisions logged в этой сессии

### 5.1 Variant (d) для wire-format расхождения Ayla ↔ Bot

**Контекст:** Ayla API spec v2.0 фиксирует `show_specialists`/`specialist_id`. Bot Формулы тела в проде 30+ дней эмиттит `show_masters`/`master_id`. Bot прибавил `recommend_services` tool которого нет в shared package.

**Рассмотренные варианты:**

| # | Approach | Pros | Cons |
|---|----------|------|------|
| (a) | Сломать Ayla wire-format под shared имена | Минимум кода, anti-hallucination в shared | Нарушение Notion spec, LLM retraining (+2-5% галлюцинаций пока учится), bot-specific term «master» drift'нёт от продуктовой терминологии Ayla |
| (b) | Переименовать shared package под Ayla spec | Spec-aligned, продуктовая терминология | Bot prod risk (БД-миграция + retrain 24-72h), x3-x5 объём работы, координация с владельцем бота, `recommend_services` всё равно local |
| (c) | Mapping shim в Ayla | Notion spec не трогаем | Две правды, dispatcher local, anti-hallucination duplicates, Variant C+ консолидация умирает |
| **(d)** ✅ | DI hook в shared, каждый consumer держит свои tools/handlers | Notion spec не трогаем, mobile не координируется, бот не страдает, anti-hallucination в shared | Локальные дубликаты остаются (но это уже факт из-за `recommend_services` в боте) |

**Решение:** (d). shared package = infrastructure layer (orchestrator + Protocol + prompts + ID validation primitives), wire-format каждый consumer выбирает сам.

**Implementation:** ayla-ai-core 0.6.0 + `tool_dispatcher: Callable | None` parameter в `AIConcierge.__init__`. Backward-compat (None → bundled `dispatch_tool_call`).

### 5.2 Privacy by-default для PII redaction (Slice B)

**До:** `chat_service` сохранял raw user content в `Message.content`, redacted text шёл в LLM. Audit-friendly «raw в DB» policy.

**После Slice B:** redacted text сохраняется в `Message.content` (privacy-by-default). Local DB / replicas не видят raw телефоны / email.

**Trade-off:** аудит «что клиент реально написал» теряется. Если потребуется back — нужен `pii_redactor` hook в AIConcierge (shared 0.7.0 follow-up).

### 5.3 Generic event row для analytics (DRF-160)

**Альтернатива:** per-event tables (FoodScanEvent, BookingEvent, etc).

**Решение:** generic `AnalyticsEvent` с `event_name: CharField` + `payload: JSONField`. Аргументы:

- Schema-on-read в BI (Metabase/Superset)
- Новые события без миграций (просто добавить в `event_catalogue.py`)
- Низкая cardinality в Phase 0/1 (~30 событий, не тысячи)

**Когда промотить в model:** когда payload-shape стабилизируется и BI начинает делать heavy aggregation на одном event_name (per-event партиции / индексы).

---

## 6. Memory updates (auto-memory system)

Created/updated:

- **NEW:** `project_ayla_ai_core.md` — extraction status (что готово в v0.6.0, что НЕ установлено в Ayla, что осталось в боте локально)
- **UPDATED:** `project_ai_chat_plan.md` — статус «PLANNED» → актуальный (DRF-240/241/230 merged, DRF-241 Slice B заменил локальный chat_service на AIConcierge)
- **UPDATED:** `MEMORY.md` — добавлена ссылка на новый файл, refresh AI Chat plan hook

---

## 7. Метрики сессии

| Метрика | Значение |
|---------|----------|
| Closed PRs (backend) | 12 |
| Closed PRs (shared) | 1 |
| Total LOC added | ~3500 (code + docs) |
| Total LOC removed | ~360 |
| New tests | 43 (backend) + 4 (shared) |
| Test runs | 173 (ai/) + 18 (tenants/) + 14 (analytics/) + smoke 84 |
| Migrations | 2 (`ai/0003`, `analytics/0001`) |
| Documentation | `docs/DRF_232_H3_SQL_PACK.md` (456 lines) |
| Shared package version bump | 0.5.0 → 0.6.0 |
| Tag created | `v0.6.0` (ayla-ai-core) |

---

## 8. Что НЕ сделано (явный scope cut)

| Задача | Почему отложено |
|--------|-----------------|
| Mobile AI Chat integration | Отдельный sprint в `beautygo-mobile` (RN), не backend-task |
| DRF-243 — bot migration на shared package | Не блокер pilot. Бот в Phase 2.4 с локальным `recommend_services`, конвергенция decoupled от M4 |
| Shared `pii_redactor` hook (0.7.0) | Privacy follow-up, не блокер. Tracked в Slice B commit message |
| MULTI_TENANT_STRICT staging + prod rollout | Step 2/3 + 3/3 — нужны 24h soak periods, отдельные PR'ы / env-var changes |
| Slim локальных дубликатов `_safe_uuid` etc | Variant (d) их оставил намеренно, полировка post-pilot |
| DRF-230 full personalisation engine | Per CEO scope reduction → Phase 6 после Decision Day verdict |
| DRF-232 SQL execution + Decision | Owner: tim lead / PO. Pack ready, awaiting 2026-05-13 |

---

## 9. Open follow-ups для следующего sprint

### 9.1 Build / CI infrastructure
- **Close docker rebuild auth gap on dev VPS** — eliminates the entire "docker compose cp + pip install" workaround in smoke workflow. Either deploy key on VPS with build-context access, or build on CI runner + push image.
- **MULTI_TENANT_STRICT staging rollout** — после 24h dev soak. Step 2/3.
- **MULTI_TENANT_STRICT prod rollout** — после 24h staging soak. Step 3/3.

### 9.2 Shared package
- **ayla-ai-core 0.7.0** — `pii_redactor` DI hook чтобы восстановить «raw в DB, redacted в LLM» policy без форка orchestrator.
- **Bot migration на shared (DRF-243)** — Phase 2.4 done будет точкой инициации. Variant (d) делает миграцию необязательной.

### 9.3 Analytics
- **BI dashboards** на `analytics_event` table — Metabase/Superset queries для North Star метрик.
- **Mobile event-emission integration** — добавить `EventLogger` в `apps/client` и `apps/pro` который шлёт события на каждый user action из catalogue.

### 9.4 H3 hypothesis validation
- **2026-05-13 Decision Day** — запустить 6 секций SQL pack на prod бота, заполнить verdict table в `docs/DRF_232_H3_SQL_PACK.md`.
- В зависимости от verdict: либо expand DRF-230 в Phase 6 (full personalisation engine), либо stay with current wire-up + pivot.

### 9.5 AI Chat
- **Mobile AI Chat screens** в `apps/client/src/screens/AIChat/` — REST endpoints live и работают через AIConcierge, мобайл не интегрировал.
- **Voice mode** (Phase 7+) — currently accepted but ignored.
- **Streaming SSE** — currently sync only.
- **Conversation summarization** когда история > 50 сообщений.

---

## 10. Состояние repo на конец сессии

```
Branch: dev (clean, no open PRs)

Last commits:
  20a762e [fix] smoke — install git for pip git+https
  fe1ceca [fix] smoke — install ayla-ai-core into running web container
  ...

Open PRs:           0
CI/CD on dev:       ✅ green
Smoke on dev VPS:   ✅ 84/84 pass
Test counts:
  - ai/             173 (was ~129 pre-session)
  - analytics/      14 (new)
  - tenants/        18
  - shared package  119 (was 115 pre-session)
```

---

## 11. Risk register (для тимлида)

| Risk | Severity | Mitigation status |
|------|----------|-------------------|
| Docker rebuild auth gap → smoke workflow гражданский костыль | M | Documented in workflow header. Untill closed, adding new dependencies в shared package требует обновления smoke pip install шага. |
| Wire-format divergence Ayla ↔ bot бесконечно | L | Variant (d) делает это OK by design. Convergence — explicit decision когда оба consumer-а согласятся на одно имя. |
| `MULTI_TENANT_STRICT` rollout staging/prod может ломать integrations с забытым X-Tenant | M | Backfill command + strict-mode 400 даёт понятный error. Auth/health/internal opt-out paths уже сконфигурированы. |
| Privacy semantic change (PII в DB) может не понравиться compliance | M | Documented in Slice B commit + tests. `pii_redactor` hook в 0.7.0 — обратимый путь. |
| 354-test breakage от env override прятался до CI run | H (предупреждение) | Lesson learned: settings/test.py всегда явно override должен любой dev-only flag, который меняет API surface. |
| Decision Day H3 verdict может уйти в "fix grounding first" | M | Pack ready, decision matrix явный. Fallback план — DRF-230 wire-up уже live, basic memory доступен без full Phase 6 build-out. |

---

## 12. Appendix — file inventory новых артефактов

```
ai/
  stores.py                                                — DjangoConversationStore (Slice A)
  concierge_factory.py                                     — get_concierge_for + helpers (Slice A→B)
  personal_context_hint.py                                 — UserPersonalContext → prompt block (DRF-230)
  migrations/0003_one_active_conversation_per_user_plus_action_index.py
  tests/test_stores.py                                     — 14 tests
  tests/test_concierge_factory.py                          — 9 tests
  tests/test_personal_context_hint.py                      — 19 tests

analytics/                                                 — NEW APP
  __init__.py
  apps.py
  event_catalogue.py                                       — 31 whitelisted event constants
  models.py                                                — AnalyticsEvent
  migrations/0001_initial.py
  serializers.py                                           — EventCreateSerializer
  views.py                                                 — AnalyticsEventView
  urls.py
  tests/test_event_endpoint.py                             — 14 tests

docs/
  DRF_232_H3_SQL_PACK.md                                   — 6-section SQL run-book (456 lines)
  SESSION_REPORT_2026-05-04_05.md                          — этот документ

ayla-ai-core (shared package, separate repo):
  src/ayla_ai_core/orchestrator.py                         — tool_dispatcher hook added
  src/ayla_ai_core/__init__.py                             — ToolDispatcher export, version 0.6.0
  pyproject.toml                                           — version bump
  tests/test_orchestrator.py                               — 4 new tests
  Tag: v0.6.0
```

---

*Report generated: 2026-05-05.*
*Generated with [Claude Code](https://claude.com/claude-code).*
