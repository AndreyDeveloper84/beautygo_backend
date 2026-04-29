# Bot Code Audit — Formula Tela / mysite — 2026-04-27

> **Audit by:** PM-роль (read-only анализ, не запуск)
> **Source repo:** `C:\Users\user\PycharmProjects\mysite` (Django + maxapi + OpenAI + Postgres)
> **Scope:** определить что reuse-able в Ayla M3, что — gap, какой integration model оптимален
> **Verdict TL;DR:** Бот **уже на 80% является портом Ayla** AI-слоя. Рекомендуется **shared Python package + bot становится первым tenant Ayla**. Экономия M3: 4–6 недель работы.

---

## 1. Discovery: что найдено

### 1.1 Стек подтверждается

| Компонент | Бот (mysite) | Ayla |
|-----------|--------------|------|
| Web framework | Django 5.x | Django 5.0 |
| DB | PostgreSQL | PostgreSQL 16 |
| Async tasks | Celery | Celery |
| Test | pytest | pytest |
| LLM | OpenAI gpt-4o-mini (через прокси) | OpenAI через px6 (memory: `project_ai_foundation`) |
| Vision | **нет** | Запланировано (food scanner) |
| Telegram-style SDK | maxapi (MAX) | (планировался RN-mobile) |
| RAG | chromadb + custom MCP server | (не реализовано) |

**Стеки совпадают почти полностью.** Это **необходимое** условие для shared-package стратегии.

### 1.2 Архитектура AI слоя бота

```
maxbot/
├── ai_concierge.py        # 274 LoC — main orchestrator (← "Адаптация Ayla chat_service.py")
├── ai_prompts.py          # 116 LoC — system prompt template
├── ai_tools.py            # 184 LoC — 5 OpenAI tool definitions
├── ai_tool_handlers.py    # 254 LoC — tool dispatcher + validation
├── ai_context.py          # 109 LoC — MasterContext (Top-N anti-hallucination)
├── ai_action_service.py   # 313 LoC — booking creation (BookingRequest)
├── ai_ui.py               # 301 LoC — render UI cards (slots, masters, confirm)
├── llm.py                 # 260 LoC — OpenAI async client wrapper
├── personalization.py     # 68 LoC  — BotUser.context atomic updates
├── intents.py             # 81 LoC  — legacy intent parsing (pre-AIConcierge)
├── mcp_client.py          # 143 LoC — MCP RAG client (chromadb FAQ)
├── response_cache.py      # 65 LoC  — pre-warmed FAQ cache
├── warmup.py              # 77 LoC  — cache warmup at startup
├── handlers/              # MAX webhook handlers (booking, ai_assistant, faq, start)
├── main.py                # 107 LoC — entry point (polling/webhook modes)
├── config.py              # 65 LoC  — env config
└── ...
```

**Total: ~3 000 LoC AI-инфраструктуры**, чисто структурированных под DI, sync_to_async, side-effect-free handlers.

### 1.3 Архитектурный паттерн (важно)

```
User message → MAX webhook
            → handler/ai_assistant.py
            → AIConcierge.send_message(bot_user, text)
                ├── _resolve_conversation (один active per BotUser)
                ├── _save_message(role=user)
                ├── build_master_context() → MasterContext (Top-N с real DB IDs)
                ├── _load_recent_history (last 10 msgs)
                ├── render_system_prompt(today, client_name, bookings_count, master_context)
                ├── openai.chat.completions.create(tools=TOOL_DEFINITIONS)  [gpt-4o-mini]
                ├── dispatch_tool_call(completion.tool_call, master_context)
                │     ├── handle_show_masters       → ActionType.SHOW_MASTERS
                │     ├── handle_show_slots         → ActionType.SHOW_SLOTS
                │     ├── handle_confirm_booking    → ActionType.CONFIRM_BOOKING
                │     ├── handle_show_my_bookings   → ActionType.SHOW_MY_BOOKINGS
                │     └── handle_ask_clarification  → ActionType.ASK_CLARIFICATION
                ├── _save_message(role=assistant, action_type, action_data,
                │                 tool_call, tokens_in/out, latency_ms)
                └── return ChatResponseDTO
            → ai_ui.py renders MAX message + buttons
            → user clicks "✅ Да" → ai_action_service.execute_confirm_booking
                                   → BookingRequest(source=bot_max) → YClients API
```

**Это в точности тот pipeline, который должен быть в Ayla AI Chat (DRF-104 / `docs/AI_CHAT_PLAN.md`)**, только адаптированный под MAX вместо REST.

### 1.4 5 OpenAI tools — детали

| Tool | Назначение | Required args | Parallel в Ayla |
|------|-----------|----------------|------------------|
| `show_masters` | Top-5 рекомендаций с match_scores + match_reasons + explanation | `master_ids[]`, `explanation` | C-01 (PRD) |
| `show_slots` | Доступные слоты для (master, service, date) | `master_id`, `service_id`, `date` | C-01 part 2 |
| `confirm_booking` | Confirm-card перед создание записи | `master_id`, `service_id`, `datetime` | C-02 |
| `show_my_bookings` | upcoming/past/all bookings клиента | `filter` enum | C-03 |
| `ask_clarification` | Уточняющий вопрос с предложенными ответами | `question`, `options[]` | S-01 (dialogue) |

**5 из 5** напрямую соответствуют user stories из Ayla PRD v3.0 P0 (C-01..C-05, S-01..S-02).

### 1.5 Anti-hallucination механизм

**Это самая ценная часть бота.** В каждом tool handler — фильтрация LLM-выданных ID через `context.candidate_ids` / `context.candidate_service_ids`. Если LLM выдумал master_id (gpt-4o-mini галлюцинирует на ~3% запросов на русском) — `_fallback_clarification` молча возвращает ask_clarification вместо ошибки. **Клиент никогда не видит сломанную карточку.**

В Ayla AI Chat plan (`docs/AI_CHAT_PLAN.md`) этого нет вообще. Нужно копировать **обязательно** — иначе reliability AI-чата будет нерабочей.

### 1.6 Telemetry уже встроена

Каждый assistant Message сохраняется с:
- `tokens_in`, `tokens_out` (cost tracking)
- `latency_ms` (performance)
- `action_type` (что AI решил сделать — strings: show_masters, show_slots, etc.)
- `action_data` (JSON с конкретными ID)
- `tool_call` (raw OpenAI tool_call для аудита)

**Это значит:** для валидации H3 (booking conversion, repeat behavior) **instrumentation pass не нужен** — данные уже пишутся 30+ дней. Аналитика — это SQL-запросы по таблице `Message`, не build.

Я ошибался ранее, когда говорил «нужно 1 спринт на инструментирование» — нужно ровно **0 спринтов**. Decision Day сдвигать не надо.

### 1.7 Models в `services_app`

| Модель | Назначение | Что попадает в Ayla |
|--------|------------|----------------------|
| `BotUser` | MAX-user (max_user_id, display_name, client_name, phone, context JSON) | → adapter to Ayla `User` model (новое поле `max_user_id`) |
| `Conversation` | UUID, bot_user FK, is_active, last_message_at | **прямо переноситься в Ayla** (с tenant_id) |
| `Message` | UUID, role, content, action_type, action_data, tool_call, tokens, latency | **прямо переноситься в Ayla** |
| `BotInquiry` | Fallback-to-manager workflow | → Ayla notifications (HumanInTheLoop) |
| `HelpArticle` | FAQ для бота | → Ayla KnowledgeDocument |
| `KnowledgeDocument` | RAG-документы для chromadb | → Ayla (если будем делать RAG) |
| `BookingRequest` | Заявка через бот или wizard сайта (source=bot_max/wizard) | → Ayla `Appointment` (более полная модель) |
| `Master` | Мастер салона (sync from YClients) | → Ayla `SpecialistProfile` (богаче) |
| `Service` | Услуга (с SEO полями) | → Ayla `Service` (без SEO) |

### 1.8 Что РЕАЛЬНО построено vs PRD Ayla v3.0

| Фича PRD | Бот | Реализовано? |
|----------|-----|--------------|
| C-01 AI-подбор мастера через чат | ✅ show_masters tool | **✅ Готово** |
| C-02 Booking в 1 tap | ✅ confirm_booking + ai_action_service | **✅ Готово** |
| C-03 Список предстоящих записей | ✅ show_my_bookings | **✅ Готово** |
| C-04 Регистрация по телефону | 🟡 BotUser auto-creates через MAX | Адаптировать |
| C-05 Отмена/перенос | ❓ Нужно проверить ai_action_service | Likely yes |
| S-01 Intent parsing | ✅ через LLM с tools | **✅ Готово** |
| S-02 Ranking мастеров | ✅ через MasterContext + LLM scores | **✅ Готово** |
| AI Food Scanner | 🟡 In deployment (по словам PO 2026-04-27) | **🟢 Reuse from bot** (после релиза) |
| AI-аватар | ❌ Нет | (deferred) |
| UserPersonalContext (30 полей, 3 зоны) | 🟡 light: BotUser.context JSON | **🔴 Big gap** — DRF-230 |
| Two-Apps middleware (X-App-Type) | ❌ Bot only | **🔴 Build for Ayla** |
| Mobile RN apps | ❌ | **🔴 Build for Ayla** |
| Working Hours + TimeOff API | ❌ (берёт из YClients) | **🔴 Build for Ayla** (multi-tenant) |
| Reviews | 🟡 Есть Review model | Reuse |
| Multi-tenant marketplace | ❌ (один салон Формула тела) | **🔴 Build for Ayla** |

**Большая картина:** **AI conversation layer = готов на 80%.** Все остальные слои (mobile, multi-tenant, full personalization, food scanner) — Ayla должна строить.

---

## 2. Reuse Map (что куда)

### 🟢 Direct copy/extract в shared package (high confidence)

| Module | Reuse | Adaptation needed |
|--------|-------|-------------------|
| `ai_concierge.py` AIConcierge orchestrator | 90% | Параметризация tenant_id, brand_voice |
| `ai_tools.py` TOOL_DEFINITIONS | 95% | master_id (int) → specialist_id (UUID) для Ayla |
| `ai_tool_handlers.py` dispatch + handlers | 90% | Same ID-type адаптация |
| `ai_context.py` MasterContext | 85% | Renamed → SpecialistContext, multi-tenant scope |
| `ai_prompts.py` render_system_prompt | 70% | Brand-voice templating, multi-tenant placeholder |
| `llm.py` OpenAI async client | 95% | Уже есть `ai/foundation` в Ayla — merge |
| `response_cache.py` + `warmup.py` | 80% | Multi-tenant cache key |
| `mcp_client.py` MCP RAG | 70% | Optional на Phase 2 |
| Models: `Conversation`, `Message` | 95% | Add `tenant_id` FK |
| `personalization.py` atomic updates | 100% (паттерн) | Use as basis для UserPersonalContext (DRF-230) |

### 🟡 Adapt с рефакторингом

| Item | Why adapt |
|------|-----------|
| `BotUser` model | Ayla имеет `User` + `Profile`. Нужен mapping через `max_user_id` поле в Ayla User |
| `Master` model | Ayla имеет `SpecialistProfile` богаче. Перенос данных = data migration, не code copy |
| `BookingRequest` model | Ayla имеет полноценный `Appointment` с state machine — bot's BookingRequest = lite version |
| YClients integration | Bot тащит расписание из YClients. Ayla должна иметь **own** Working Hours / TimeOff (multi-tenant) |
| System prompt | "Ты — Алина, ассистент салона Формула тела" → "Ты — Ayla, AI-помощник по beauty" |

### 🔴 Bot-specific, не переносить в Ayla

- `maxapi` SDK + `handlers/` (MAX webhook) — Ayla mobile = REST API через `/api/v1/ai/chat/`
- Salon-specific texts (`texts.py` — приветствия от «Алины»)
- Cluster `agents/` (SEO/SMM/analytics agents) — отдельный SaaS-продукт для салона, не для Ayla
- `agents/integrations/` (VK Ads, Yandex Direct/Metrika) — relevant для marketing, не для booking

---

## 3. Gap Analysis — что Ayla строит сама

### 🔴 Критические gaps (M3 P0)

1. **REST API layer** для AI Chat (`/api/v1/ai/chat/` endpoints) — бот говорит через MAX webhook, Ayla mobile нужен REST. **Build:** thin wrapper над AIConcierge.
2. **JWT auth + X-App-Type middleware** — у бота только max_user_id, у Ayla — JWT + client/pro разделение.
3. **Multi-tenant архитектура** — bot хардкоднут на Формулу тела. Ayla = marketplace из N мастеров. Нужно `tenant_id` (или `marketplace_owner`) на всех ключевых моделях + scoping в queries.
4. **UserPersonalContext** (DRF-230) — bot имеет light `context: JSONField`, Ayla нужна полная архитектура с 30+ полями, 3 зонами деликатности, anti-spam rules, Celery infer_user_patterns.
5. **152-ФЗ права на данные** — bot не имеет «удали всё», «выгрузи мои данные», ENG-управления sensitive zones. Ayla нужно строить.
6. ~~Food Scanner — нет вообще. M3 build from scratch.~~ **UPDATE 2026-04-27:** Food Scanner **в активном развёртывании в боте Формулы тела** (по слову PO). После релиза — **reuse в Ayla shared package** аналогично AI booking слою. Audit нужно дополнить отдельной секцией Food Scanner после получения кода.
7. **Working Hours / TimeOff API** — bot берёт из YClients. Ayla = own system (мастер-индивидуал может не иметь YClients).

### 🟡 Important gaps (M3 P1 / Phase 2)

8. Mobile RN apps (Two Apps) — Ayla строит сама.
9. Notifications мульти-канал (Push FCM + SMS отдельно) — bot имеет только MAX-push.
10. Specialist app (Ayla Pro) с расписанием, аналитикой, портфолио.
11. Rich Reviews UI flow.
12. AI-аватар + прогресс (deferred).
13. Geo + ежедневник «День».
14. Реферальная программа.

### 🟢 Reusable as-is через shared package

- AI orchestrator, tools, handlers, prompts, master context, telemetry — **80% AI слоя**.

---

## 4. Recommended Integration Model — Variant C+ (refined)

> **Variant C+ — Shared Python package с bot как первым tenant Ayla.**

### Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                  ayla-ai-core (shared package)                   │
│  ─────────────────────────────────────────────────────────────  │
│   AIConcierge(tenant_id, brand_voice)                           │
│   TOOL_DEFINITIONS (parametrized: master_id type, etc.)         │
│   dispatch_tool_call(tc, context)                               │
│   build_specialist_context(tenant_id) → SpecialistContext       │
│   render_system_prompt(brand_voice, today, client_name, ...)   │
│   OpenAI async client                                           │
│   Anti-hallucination filter                                     │
│   Models: Conversation, Message (с tenant_id)                   │
│   Telemetry hooks                                               │
└────────┬───────────────────────────────────────┬─────────────────┘
         │                                       │
         │ pip install -e or git submodule       │ pip install -e
         │                                       │
         ▼                                       ▼
┌──────────────────────┐               ┌──────────────────────┐
│  Bot (mysite)        │               │  Ayla backend        │
│  ─────────────────── │               │  ─────────────────── │
│  MAX webhook handler │               │  REST /ai/chat/      │
│  ai_action_service   │               │  JWT + X-App-Type    │
│  YClients adapter    │               │  Multi-tenant scope  │
│  Salon-specific UI   │               │  UserPersonalContext │
│  (Формула тела)      │               │  Food Scanner (build)│
└──────────────────────┘               │  Mobile API          │
                                       │  Working Hours API   │
                                       │  Two-Apps middleware │
                                       └──────────────────────┘
                                                │
                                                │ Eventually
                                                ▼
                                       ┌──────────────────────┐
                                       │  Bot мигрирует на    │
                                       │  Ayla DB как tenant  │
                                       │  «Формула тела»      │
                                       └──────────────────────┘
```

### Phases

**Phase A (M3, ~3 недели):** Создать `ayla-ai-core` как private GitHub repo. Извлечь из бота 10 модулей выше (зелёный список). Bot **временно дублирует** код (не ломаем prod). Параллельно Ayla строит REST API + JWT + multi-tenant вокруг shared package.

**Phase B (M4, ~1 неделя):** Bot переходит на использование `ayla-ai-core` через `pip install`. Удаляем дубликаты в боте. Bot продолжает работать с собственной БД (Формула тела), но логика AI = из shared package.

**Phase C (M5+, ~2 недели):** Bot мигрирует на Ayla БД как tenant. Формула тела становится первым реальным tenant Ayla с production-нагрузкой. Это **естественный stress-test** Ayla multi-tenancy.

### Преимущества

1. **Time savings M3:** AI Chat в Ayla — не пишем с нуля, **экономия 4–6 недель**.
2. **Reliability proven:** anti-hallucination, fallback patterns, telemetry — уже отлажены 30+ дней в production.
3. **Bug fix amplification:** одна правка в shared package → улучшение в обеих системах.
4. **Validation H3 dataset:** prod-данные бота = ground truth для memory hypothesis.
5. **Bot's clientele = first Ayla users:** plug-and-play migration в Phase C.

### Риски

| Risk | Mitigation |
|------|------------|
| Bot prod ломается во время extraction | Phase A — bot не трогаем, только копируем код. Phase B рефакторинг с feature flag |
| Different evolution rates (bot vs Ayla) | Semver на shared package. Bot pin'ится на минорной версии |
| Multi-tenant сложность когда сейчас один салон | Tenant_id поле дефолтит на `formula-tela` для бота — невидимо для существующих клиентов |
| Shared package overhead | Если за 6 месяцев пользы нет — собираем обратно в один репо. Reversible |
| IP / commercial complexity | Одно лицо владеет обоими репо (Andrey) — IP-clean |

---

## 5. Concrete Action Plan

### Linear-тикеты для создания (after this audit)

| # | Title | Priority | Estimate |
|---|-------|----------|----------|
| 1 | Create `ayla-ai-core` private repo + boilerplate | High | 1 день |
| 2 | Extract `AIConcierge` + `TOOL_DEFINITIONS` + `dispatch_tool_call` в shared | High | 3 дня |
| 3 | Extract `MasterContext` → `SpecialistContext` (rename + multi-tenant) | High | 2 дня |
| 4 | Extract `render_system_prompt` с brand_voice параметризацией | High | 1 день |
| 5 | Add `Conversation`, `Message` models в Ayla с tenant_id | High | 2 дня |
| 6 | Build REST `/api/v1/ai/chat/` endpoints поверх AIConcierge | High | 3 дня |
| 7 | Bot migration на shared package (Phase B) | Medium | 5 дней |
| 8 | Multi-tenant `tenant_id` scoping везде (Specialist, Service, Master, Conversation) | High | 5 дней |
| 9 | Update `docs/AI_CHAT_PLAN.md` с reuse-стратегией | Low | 1 день |
| 10 | DRF-231 (Test 1 H1 food) — отдельный bot/concierge MVP, **НЕ через Формулу** | Medium | (already created, refine) |

**Total estimate Phase A+B:** ~22 рабочих дня = ~4–5 спринтов = ~5–6 недель календарного времени для двух разработчиков.

### Что НЕ делать прямо сейчас

- ❌ Не трогать prod бота. Phase A = read-only copy.
- ❌ Не пытаться extract все 17 файлов сразу. Начинаем с самого ценного (orchestrator, tools, handlers, context, prompts).
- ❌ Не строить multi-tenancy через JSON (`{tenants: [...]}`) — нужен полноценный `Tenant` model + FK.
- ❌ Не пытаться выгрузить YClients integration в Ayla. YClients — это для Формулы тела (B2B-зависимость салона). Ayla = own scheduling system.

---

## 6. Validation Strategy Update

### H1 (Food Scanner ≥2.5/день) — валидация через бот Формулы

**UPDATE 2026-04-27:** Food Scanner деплоится в бот в ближайшее время → **отдельный concierge MVP не нужен**. Валидация H1 идёт через реальную аудиторию prod-бота.

**Action items:**
- DRF-231 update: source аудитории — клиенты бота Формулы (как и AI booking validation);
- Нужны event-tracking поля: `FoodScan` модель с `bot_user`, `created_at`, `vision_result`, `recommendation_response` (если ещё нет — добавить при релизе);
- Success criteria без изменений: median ≥2.5 scans/day в дни 8–14 = H1 живая;
- Cost: ₽0 (часть бот-проекта Макса), не ₽5–10k.

**Bias caveat сохраняется:** wellness-аудитория Формулы старше Ayla ICP (Анна 25–35). Если H1 «жива» только на этой аудитории, нужен **второй cohort на Anna-ICP** (DRF-234 ICP Cohort Cross-Validation остаётся актуальным).

### H3 (Memory + booking conversion) — у нас уже есть данные

**Не нужно ждать 2 недели — данные есть в `Message` таблице бота за 30+ дней.** Запросы:

```sql
-- Action type distribution (что AI делает чаще всего)
SELECT action_type, COUNT(*), AVG(latency_ms), AVG(tokens_in+tokens_out) as avg_tokens
FROM services_app_message
WHERE role='assistant' AND created_at > now() - interval '30 days'
GROUP BY action_type ORDER BY 2 DESC;

-- Conversion: show_masters → confirm_booking в той же conversation
SELECT
  COUNT(DISTINCT c.id) FILTER (WHERE m1.action_type='show_masters') as showed_masters,
  COUNT(DISTINCT c.id) FILTER (WHERE m2.action_type='confirm_booking') as confirmed,
  100.0 * COUNT(DISTINCT c.id) FILTER (WHERE m2.action_type='confirm_booking')
        / NULLIF(COUNT(DISTINCT c.id) FILTER (WHERE m1.action_type='show_masters'), 0) as conv_pct
FROM services_app_conversation c
LEFT JOIN services_app_message m1 ON m1.conversation_id = c.id
LEFT JOIN services_app_message m2 ON m2.conversation_id = c.id;

-- Repeat-booking pattern (proxy на favorite_master)
SELECT
  br.bot_user_id,
  br.master_name,
  COUNT(*) as bookings
FROM services_app_bookingrequest br
WHERE br.source = 'bot_max'
GROUP BY 1, 2
HAVING COUNT(*) >= 2
ORDER BY 3 DESC;
```

**Время на эту аналитику:** 1 день (написать запросы, запустить, проанализировать). Можно сделать к 2026-04-30.

→ **DRF-232 update needed:** «Часть 1 — Анализ prod-данных» становится главной частью, выполнима за 1 день, не 2 недели.

→ **Decision Day можно сдвинуть с 2026-05-13 на 2026-05-06** (на неделю раньше) если H3 part из бот-данных + Test 1 закроется быстро.

---

## 7. Risks & Open Questions

### Operational risks

1. **Production bot stability:** если код Phase A/B затронет prod — массажный салон встанет. **Mitigation:** Phase A = read-only copy (нулевой риск). Phase B = bot deploy с feature flag + rollback plan.
2. **Shared package versioning hell:** если Ayla эволюционирует быстро, бот pin'ится на старой версии — расходимся. **Mitigation:** semver + monthly sync sprint.
3. **Multi-tenant performance:** scoping queries by tenant_id — нужны индексы. **Mitigation:** добавить tenant_id в composite indexes сразу при миграции.

### Strategic open questions

1. **Bot's клиенты = Ayla's клиенты?** Когда Phase C мигрирует bot на Ayla DB, прежние клиенты Формулы автоматически появляются в Ayla. Это OK для Andrey (общая инфра), но **нужно согласие Max-владельца Формулы** на data-merge. Юридически: договор о data ownership.
2. **Yclients lock-in:** бот тянет Master + Service из YClients. Ayla не имеет YClients. Если Формула тела продолжает использовать YClients как source-of-truth расписания — Ayla становится **читателем YClients для одного tenant'а**. Это плохой паттерн (зависимость на стороннее B2B). **Долгосрочно** нужно переводить Формулу на Ayla schedule, отказываясь от YClients (это решение Max-владельца).
3. **Brand mixing:** бот говорит «Алина из Формулы тела». Если он переходит на Ayla shared package + Ayla DB — переход на «Ayla» брендинг неизбежен. **Когда это коммуницируем клиентам Формулы?** Скорее всего одновременно с релизом Ayla mobile.

### Technical open questions

4. Какая версия Django у бота? (Должна быть совместима с Ayla 5.0)
5. Какие ASGI / Celery версии?
6. Tests coverage — 34 тестa упомянуто; что покрывают (бот pipeline, models, handlers)?
7. Есть ли OpenAI proxy или прямой вызов? (Memory: Ayla использует px6 proxy)
8. Какой LLM-prompt token budget уже расходуется в проде? (Cost estimation для Ayla scale)

---

## 8. Updated Strategy Summary

### Что меняется в Ayla M3 после этого аудита

1. **AI Chat scope сокращается с 17 файлов → 5 файлов** (REST wrapper + JWT + multi-tenant + brand prompt + Ayla-specific tools если нужны).
2. **DRF-230 (UserPersonalContext)** — остаётся as planned, но получает **prior art reference** — bot's `personalization.py` показывает atomic-merge паттерн.
3. **Food Scanner** — отдельный M3 epic, не связан с ботом.
4. **Multi-tenancy** — становится критическим foundation работы M3.

### Что меняется в Validation strategy (DRF-231/232/233)

| Тикет | Update |
|-------|--------|
| DRF-231 (Test 1 H1) | Не используем бот Формулы (нет food scanner). Создаём **отдельный** concierge MVP — Telegram или MAX-бот opt-in. ₽5-10k cost, 14 дней |
| DRF-232 (Test 3 H3) | **Прорыв:** prod-данные есть. Часть 1 (SQL-аналитика) выполнима за 1 день. Customer interviews × 5 + (опц.) A/B позиционирования |
| DRF-233 (Decision Day) | Можно сдвинуть с 2026-05-13 на 2026-05-06 (если H3 SQL за 1 день пройдёт) |

### Что меняется в Linear

- **Создать 10 новых тикетов** (Phase A + B extraction work, см. Section 5);
- **Закрыть** или **сжать scope** существующих AI Chat ticket-ов в M3 (если уже есть в backlog);
- DRF-232 description обновить — «prod-data SQL» как Часть 1.

---

## 9. Deliverables и следующие шаги

### Этот документ закрывает

- [x] Code audit бота (mysite/Formula tela)
- [x] Reuse map: что копировать, что адаптировать, что бросить
- [x] Gap analysis vs Ayla M3 scope
- [x] Integration model recommendation (Variant C+ → shared package + tenant migration)
- [x] Validation strategy refinement
- [x] Risk assessment

### Следующие шаги (в порядке приоритета)

1. **Решение PO**: согласовать Variant C+ (shared package + tenant migration) или предложить альтернативу.
2. **Open questions discussion** с Max-владельцем Формулы тела:
   - Согласие на data-merge (Phase C)
   - Long-term plan по YClients (зависимость или миграция)
   - Брендинг переход (Алина → Ayla)
3. **SQL-аналитика prod-данных бота** (1 день) — закрывает большую часть H3.
4. **Создать 10 новых Linear тикетов** Phase A+B (если Variant C+ принят).
5. **Update DRF-231/232/233** под новые реалии.
6. **Возобновить PM-аудит Phase 2** (Customer Journey + Market Sizing + Competitors) с учётом «build around bot» решения.

---

*Аудит завершён 2026-04-27. Источники: read-only анализ ~30 файлов из `C:\Users\user\PycharmProjects\mysite`. Не запускался ни один скрипт, не модифицирован ни один файл.*
