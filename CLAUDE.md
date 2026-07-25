# CLAUDE.md — Ayla Project Intelligence
> **Источник истины для AI-ассистентов.** Держит только то, что нужно на КАЖДОЙ задаче: правила, гейтчи, статусы, указатели. Детали (code-примеры, деревья, диаграммы) — в `docs/`, не дублируй сюда. Карта доков — в конце файла.

---

## 📋 QUICK REFERENCE
```
Проект:      Ayla — AI Life Assistant (beauty = точка входа; daily-hook = food + memory)
Позиционир:  «AI, который помнит. Всегда.» (long-term personal memory = главное преимущество)
Архитектура: Two Apps (Ayla 🟢 client + Ayla Pro 🟣 specialist)
Пилот:       Пенза → Казахстан (Phase 5)
Backend:     Python 3.12+ / Django 5.0 + DRF · PostgreSQL 16 · Redis 7 · Celery
AI:          Claude Sonnet 4 (Anthropic)+ OpenAI GPT-4 Vision (food scanning)
Mobile:      React Native (репо beautygo-mobile, rename→ayla-mobile pending)
Запуск:      make up (Docker) / make init (первый раз) · Тесты: make test · Lint: make lint
```

### 🔑 Key Headers
```http
X-App-Type: client   # 🟢 Ayla requests
X-App-Type: pro      # 🟣 Ayla Pro requests
```

| Приложение | Bundle ID | Аудитория | X-App-Type | Deep link |
|------------|-----------|-----------|------------|-----------|
| 🟢 **Ayla** | `ru.ayla.client` | Клиенты | `client` | `ayla-client://` |
| 🟣 **Ayla Pro** | `ru.ayla.pro` | Мастера | `pro` | `ayla-pro://` |

**Роли:** Client (🟢 ищет/бронирует через AI) · Specialist (🟣 расписание/услуги/клиенты) · Admin (web).
Shared package `@beautygo/shared` (→ `@ayla/shared` pending): API-клиент + модели + auth + i18n.

---

## 🎯 PROJECT (кратко)
Ayla — AI-ассистент качества жизни для женщин 20–45. Beauty-запись = точка входа и монетизация;
ежедневный retention — на AI-фичах (Food Scanner, аватар, персональные рекомендации).
**Дифференциатор = долгосрочная личная память** (UserPersonalContext): каждый разговор умнее предыдущего.

Полное видение / roadmap / killer-scenario / 5-tab навигация → `docs/PRD_Ayla_Killer_Scenario_v1.0.md`,
`docs/MVP_ROADMAP_2026-07.md`, `docs/00 Foundation/`.

---

## 🔑 КРИТИЧНЫЕ ГЕЙТЧИ (помнить ВСЕГДА)

### API / контракты
- **X-App-Type обязателен** на каждом запросе. Middleware: 403 `WRONG_APP_TYPE` при отсутствии/несоответствии endpoint↔app_type. Не все endpoints доступны обоим приложениям.
- **JWT-поля в ответах:** `access_token` / `refresh_token` (**НЕ** `access`/`refresh`).
- **Payment field:** `external_id` (**НЕ** `provider_payment_id`).
- Формат ответов: `{data, meta}` / ошибки `{error:{code, message, details}}`.

### Payments (YooKassa) — реализовано
PaymentStatus mapping (internal → API spec):

| Internal | API | | Internal | API |
|----------|-----|-|----------|-----|
| `pending` | `pending` | | `failed` | `failed` |
| `authorized` | `pending` | | `refunded` | `refunded` |
| `paid` | **`succeeded`** | | `partially_refunded` | `refunded` |

Endpoints: `POST /payments/create/` 🟢 · `GET /payments/{id}/` ⚪ · `POST /payments/webhook/` ⚪(AllowAny) · `POST /payments/{id}/refund/` 🟢.
Env: `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, `YOOKASSA_AGENT_ID` (split, optional).
Two-stage hold→capture; webhook idempotency через `last_webhook_event_id` + `X-Request-Id`. Детали → `docs/DEV_REFERENCE.md`.

### Booking Engine (DDD, `appointments/`)
State machine:
```
pending → awaiting_payment → confirmed → completed
                          └→ cancelled
confirmed → cancelled | no_show
```
- **ACTIVE_BOOKING_STATUSES** (держат слот): `{pending, awaiting_payment, confirmed}`
- **Terminal:** `{completed, cancelled, no_show}`
- Константы: `BOOKING_COMMISSION_PERCENT=8.0` · `BOOKING_MIN_AHEAD_MINUTES=60` · `BOOKING_MAX_AHEAD_DAYS=60` · `BOOKING_SLOT_GRID_MINUTES=30`.

### ⚠️ MVP-ограничения (не забывать при отладке)
1. **Outbox worker не запущен** — события пишутся в `OutboxEvent`, но не обрабатываются (нужен Celery+Redis).
2. **LocMemCache** вместо django-redis (заменить для prod).
3. **`select_for_update()` — no-op на SQLite** — concurrency-тесты требуют PostgreSQL.
4. **Notifications / AI Chat** — не реализованы.

### Критичные бизнес-правила
1. Слоты: интервал 30 мин, минимум за 1 час до записи.
2. Отмена: бесплатно за 24+ ч, 50% за 2–24 ч, 100% за <2 ч.
3. Рейтинг: денормализован, пересчёт синхронно после каждого отзыва.
4. Комиссия: 8% с онлайн-платежей.

### Reviews — реализовано
OneToOne (один отзыв на запись, 409 `REVIEW_EXISTS`); `is_anonymous`→`client_name=null`; `is_hidden` исключает из листинга и рейтинга; пагинация 20/max 100, `?sort=recent|rating`.

---

## 📝 STANDARDS
- Type hints обязательны. Docstrings для публичных методов. Транзакции для связанных операций. Логировать (`logging`, **не** `print`). Кешировать тяжёлые запросы. Валидировать вход.
- **НЕ** хардкодить секреты (`.env`) · **НЕ** коммитить данные в миграциях (только структура) · **НЕ** `Model.objects.create()` в views (используй services) · **НЕ** писать бизнес-логику в serializers (только валидация).

| Тип | Стиль | | Тип | Стиль |
|-----|-------|-|-----|-------|
| Класс | PascalCase | | Константа | SCREAMING_SNAKE |
| Функция/переменная | snake_case | | Модуль | snake_case |
| URL path | kebab-case | | | |

Полные примеры (style, serializers, views, services, celery) → `docs/coding-standards.md`. Тесты → `docs/testing.md`.

---

## 📊 SPEC ALIGNMENT STATUS
> Источник: API Spec v2.0 (Notion) + PRD v3.0.

| Секция | Статус | | Секция | Статус |
|--------|--------|-|--------|--------|
| Auth (OTP/Anon/Social) | ✅ | | Payments (YooKassa) | ✅ |
| Users (/me, delete) | ✅ | | Search | ✅ basic |
| Specialists (catalog) | ✅ | | Notifications | ❌ M3 P0 |
| Services (CRUD + cat) | ✅ | | AI Chat (Claude) | ❌ M3 P0 |
| Appointments (+state machine) | ✅ | | **UserPersonalContext** | ❌ **M3 P0 (load-bearing)** |
| Working Hours + TimeOff | ✅ | | Favorites / Analytics | ❌ |
| Reviews | ✅ | | Food Scanner | 🟡 Slice 1 |
| | | | AI-аватар + прогресс | 🟠 Phase 2 (deferred) |

---

## 🏷️ BRAND MIGRATION (BeautyGO → Ayla)
Каноническое название = **Ayla**. Миграция кода/инфры поэтапная. DNS rebrand отложен (backend на `gobeauty.site` до покупки Ayla-домена).

**Правила для нового кода:**
1. Любая новая user-facing строка (push/SMS/email/UI) — «Ayla» / «Ayla Pro».
2. Любые новые URL/идентификаторы — `ayla.*` / `ru.ayla.*` / `ayla-*://`.
3. Существующий код **не** переписывать «по пути» — только в рамках rebrand-тикетов.
4. PR-ревью блокирует новый код с «BeautyGO»-строками или `beautygo` URL/id.
5. Коммиты на английском, нейтрально, без «BeautyGO».

Полная таблица pending-миграции → `docs/DEV_REFERENCE.md` / история в git.

---

## 🗺️ КАРТА ДОКОВ (указатели — читать при работе с подсистемой)
| Тема | Документ |
|------|----------|
| Детальный dev-справочник (структура, диаграммы, code) | `docs/DEV_REFERENCE.md` |
| Coding standards (примеры) | `docs/coding-standards.md` |
| Testing (fixtures, factories, примеры) | `docs/testing.md` |
| Booking lifecycle (canonical) | `docs/04 Domain Models/Booking Lifecycle Specification.md` |
| Booking dual-mode source | `docs/architecture/booking-source-dual-mode.md` |
| Payments capture strategy | `docs/architecture/payments-capture-strategy.md` |
| Personal Context internal API (FROZEN) | `docs/PERSONAL_CONTEXT_INTERNAL_API_CONTRACT.md` |
| AI Chat plan | `docs/AI_CHAT_PLAN.md` |
| Food Scanner decision | `docs/FOOD_SCANNER_DECISION.md` |
| Multi-tenant | `docs/MULTI_TENANT.md` |
| Observability | `docs/OBSERVABILITY.md` |
| Проектный индекс экосистемы (5 репо) | `docs/PROJECT_INDEX.md` |
| Каноническая база знаний (Constitution, ADR, MOC) | `docs/00 Foundation/`, `docs/05 Architecture/` |
| Roadmap / PRD / видение | `docs/MVP_ROADMAP_2026-07.md`, `docs/PRD_Ayla_Killer_Scenario_v1.0.md` |

**Контакты:** Project Owner — Andrey. PRD / API Spec / Schema — Notion.

---

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. The
skill has multi-step workflows, checklists, and quality gates that produce better
results than an ad-hoc answer. When in doubt, invoke the skill. A false positive is
cheaper than a false negative.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke /office-hours
- Strategy, scope, "think bigger", "what should we build" → invoke /plan-ceo-review
- Architecture, "does this design make sense" → invoke /plan-eng-review
- Design system, brand, "how should this look" → invoke /design-consultation
- Design review of a plan → invoke /plan-design-review
- Developer experience of a plan → invoke /plan-devex-review
- "Review everything", full review pipeline → invoke /autoplan
- Bugs, errors, "why is this broken", "wtf", "this doesn't work" → invoke /investigate
- Test the site, find bugs, "does this work" → invoke /qa (or /qa-only for report only)
- Code review, check the diff, "look at my changes" → invoke /review
- Visual polish, design audit, "this looks off" → invoke /design-review
- Developer experience audit, try onboarding → invoke /devex-review
- Ship, deploy, create a PR, "send it" → invoke /ship
- Merge + deploy + verify → invoke /land-and-deploy
- Configure deployment → invoke /setup-deploy
- Post-deploy monitoring → invoke /canary
- Update docs after shipping → invoke /document-release
- Weekly retro, "how'd we do" → invoke /retro
- Second opinion, codex review → invoke /codex
- Safety mode, careful mode, lock it down → invoke /careful or /guard
- Restrict edits to a directory → invoke /freeze or /unfreeze
- Upgrade gstack → invoke /gstack-upgrade
- Save progress, "save my work" → invoke /context-save
- Resume, restore, "where was I" → invoke /context-restore
- Security audit, OWASP, "is this secure" → invoke /cso
- Make a PDF, document, publication → invoke /make-pdf
- Launch real browser for QA → invoke /open-gstack-browser
- Import cookies for authenticated testing → invoke /setup-browser-cookies
- Performance regression, page speed, benchmarks → invoke /benchmark
- Review what gstack has learned → invoke /learn
- Tune question sensitivity → invoke /plan-tune
- Code quality dashboard → invoke /health
