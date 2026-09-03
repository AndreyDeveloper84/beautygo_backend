# Индекс проекта BeautyGO

Дата снимка: 2026-07-18. Собран по фактическому состоянию локальных рабочих копий (не по удалённым веткам).

> Важно: локальные checkout-ы находятся на feature-ветках активного трека **Memory Foundation**, а не на dev/main. Расхождения — в разделе 6.

---

## 1. Обзор экосистемы

| Компонент | Локальный путь | Remote | Роль | Текущая ветка | Состояние |
|---|---|---|---|---|---|
| **beautygo_backend** (Ayla) | `Ayla/djangoproject` | `AndreyDeveloper84/beautygo_backend` | Основной монолит: бронирования, платежи, каталог, пользователи | `feat/memory-foundation-internal-api` | +2/−9 от `dev` |
| **ai-bot-platform** | `ai-bot-platform` | `AndreyDeveloper84/ai-bot-platform` | AI-бот: каналы (MAX, Telegram), оркестрация диалога, RAG, Mini App API | `feat/memory-consent-global` | +1/−20 от `origin/dev` |
| **ayla-ai-core** | `ayla-ai-core` | `AndreyDeveloper84/ayla-ai-core` | Shared-библиотека AI-оркестрации (не сервис) | `feat/memory-context-builder` | +1/−2 от `main` |
| **frontAyla** | `Ayla/frontAyla` | `Shiro-Py/frontbeauty` | Мобильные приложения client + pro (Expo RN) — **этап 2** по roadmap | `dev` | +31/0 от `main`, затишье ~3 мес (планово, этап 2) |
| **ayla-knowledge** | `Ayla/ayla-knowledge` (worktree агента: `Ayla/ayla-knowledge-agent`) | `AndreyDeveloper84/ayla-knowledge` | Каноническая база знаний (= корень Obsidian Vault): Constitution, Knowledge Architecture Spec, Decision Log | `main` (+ `agent/*`, `review/*`) | наполняется; schema v1.6, валидатор + review-gate |
| ~~Obsidian Vault (старый)~~ | `Documents/Obsidian Vault` | — | legacy: мигрирует в ayla-knowledge по одному документу | — | 26 заметок, переносится |

frontAyla — 4-й репозиторий проекта (аккаунт `Shiro-Py/frontbeauty`); владение подтверждено владельцем 2026-07-18. ayla-knowledge — 5-й репозиторий; правила работы: ветки `agent/<document>`, один документ = один commit, `python scripts/validate_knowledge.py` + unittests перед push, merge после review.

---

## 2. Карта интеграций

```
┌─────────────────┐   HTTPS, JWT + X-App-Type: client|pro
│   frontAyla     │ ────────────────────────────────────────►
│ (client / pro)  │        https://dev.gobeauty.site/api/v1
└─────────────────┘                                          │
                                                             ▼
┌──────────────────┐  Bearer AYLA_INTERNAL_API_TOKEN   ┌─────────────────┐
│  ai-bot-platform │ ─────────────────────────────────►│ beautygo_backend │
│  (бот, Mini App) │  /api/v1/internal/{users,specialists,│  (Django)       │
│                  │   services,appointments,personal-    │                 │
│                  │   context}                           │                 │
│                  │◄───────────────────────────────── │                 │
└────────┬─────────┘  outbox → POST /api/v1/internal/    └───────┬─────────┘
         │             events/ingest (HMAC-SHA256, beat 30с)     │
         │                                                       │
         └─────────── shared lib: ayla-ai-core ──────────────────┘
                      (оба пинят по SHA d72a5de4, DRF-1441)

Legacy/внешние:
- formulatela58.ru → ai-bot-platform (синк каталога, MYSITE_CATALOG_*)
- YClients → beautygo_backend (только в dev, в текущей ветке отсутствует)
- OpenAI/Anthropic (ai-core адаптеры), YooKassa, sms.ru, FCM, Yandex Vision, USDA
```

Потоки данных:
- **frontAyla → backend**: единственный мобильный клиент; auth (OTP/social/anonymous JWT), каталог, слоты, записи, отзывы, избранное.
- **бот → backend**: внутренние API с Bearer-токеном — бронирование, каталог, профиль, personal-context (frozen contract v1.0).
- **backend → бот**: доменные события через outbox (appointments), подпись HMAC-SHA256; гейт по топикам `OUTBOX_EXTERNAL_DELIVERY_TOPICS` (по умолчанию пуст — доставка выключена).
- **ayla-ai-core**: Python-библиотека, не сеть; оба backend пинят по SHA коммита, бампы парные.

---

## 3. Карточки репозиториев

### 3.1 beautygo_backend — `Ayla/djangoproject`

- **Назначение:** каноническое состояние — жизненный цикл бронирования, платежи (YooKassa), каталог услуг, идентичность + PII. Половина split-domain архитектуры (ADR-0009). Инфра-имена `gobeauty.site`, бренд — Ayla.
- **Стек:** Python 3.12, Django 5.2.5, DRF 3.16.1, PostgreSQL 16 (prod) / SQLite (dev), Redis 7 (Celery + cache), Celery beat (11 периодических задач), MinIO/S3, gunicorn.
- **Ключевые модули:** `users/` (auth OTP + social, профили, personal context, internal API), `appointments/` (слот-движок, outbox, idempotency), `payments/` (YooKassa, webhook, refund, 54-ФЗ), `services/` (каталог), `ai/` (AI-чат, concierge_factory, LLM proxy), `nutrition/` (food scanner, дневники, cross-domain), `notifications/` (FCM push, напоминания), `tenants/` (мультитенантность, permissive-режим), `reviews/`, `analytics/`, `search/`.
- **API:** всё под `/api/v1/`; публичные `auth/`, `users/`, `services/`, `specialists/`, `appointments/`, `payments/`, `ai/`, `nutrition/` и др.; обязательный `X-App-Type: client|pro`; internal API для бота — см. карту интеграций. Docs: `/api/docs/`.
- **Запуск:** `pip install -r requirements.txt` → `migrate` → `runserver`; или `docker-compose up -d` (web/db/redis/celery/minio). CI: lint+pytest на PR, push в `dev` → деплой на VPS.
- **Тесты:** ~136 файлов, pytest + pytest-django, только users+appointments+nutrition ≈ 1148 тест-функций.
- **Доки:** `docs/` (~40+ документов: ADR-0009, MVP_ROADMAP_2026-07, PERSONAL_CONTEXT_INTERNAL_API_CONTRACT v1.0, MULTI_TENANT, runbooks) + PRD/бизнес-документы в корне. Незакоммиченная реструктуризация базы знаний (`docs/00 Foundation/` … `docs/10 Operations/`) лежит untracked.
- **Состояние:** ветка реализует internal personal-context API для бота (M-B1, A1a + фриз контракта). A1b (consent backstop, MemoryFact) не сделан. Отстаёт от dev на 9 коммитов (нет YClients intake S3A/S3C/S3D, #205–#215) — нужен rebase перед интеграцией.

### 3.2 ai-bot-platform

- **Назначение:** AI-бот платформы: консультации, маршрутизация интентов, RAG по базе знаний, каналы MAX/Telegram, Mini App API (клиент/мастер/админ), событийная интеграция с Ayla. Заменяет замороженный `formula_tela/mysite/maxbot/` (миграция по спринтам, ADR-0002/0009).
- **Стек:** Python 3.12, Django 5.2 + DRF, Celery + beat, PostgreSQL 16, Redis 7 (обязателен в проде), ChromaDB (KB/RAG), S3/MinIO (replay), `uv` как пакетный менеджер, OpenTelemetry, Sentry. LLM: OpenAI + Anthropic, мульти-провайдерная маршрутизация (ADR-0005).
- **Ключевые модули (apps/, ~38):** `tenancy/` (мультитенантность, contextvar), `identity/` (BotUser, MemoryEntry, единый `/api/v1/me`), `conversations/`, `orchestrator/`, `skills/` + `tools/` (booking, faq, privacy_consent), `llm/`, `kb/` (RAG, ChromaDB, сидирование из Google Docs), `channels/` + `ingress/` (MAX, Telegram), `eventbus/` (ingest событий от Ayla, идемпотентность, DLQ), `consent/` (ConsentRecord, memory-consent — текущая ветка), `booking/` (RemoteBookingProxy → Ayla REST), `master_api/`, `admin_api/`, `miniapp_api/`, `internal_chat/`, `marketplace/`, `catalog/` (зеркало), `replay/`, `audit/`, `observability/`, `promptreg/`, `persona/`, `experiments/`.
- **Запуск:** `uv sync --extra dev` → `migrate` → `runserver`; полный стек `make up` (web:8000, worker, postgres, redis, chromadb:8001, minio). Деплой: push в `dev`/`main` → GitHub Actions.
- **Тесты:** 366 тестовых файлов (per-app + cross-cutting: smoke/integration/e2e/contracts/tenancy), pytest.
- **Доки:** `README.md`, `docs/architecture.md`, ADR-0001…0011, `docs/plans/` (спринты, MEMORY_* спеки), `docs/runbooks/` (~27), `docs/specs/memory-entry-schema.md`.
- **Состояние:** ветка `feat/memory-consent-global` — глобальное согласие на память (memory_green/yellow/red + withdraw cascade). ~60 untracked файлов (планы памяти, investigations). Локальный checkout отстаёт от `origin/dev` на 20 коммитов — синхронизировать перед работой.

### 3.3 ayla-ai-core

- **Назначение:** shared AI-оркестрационное ядро (библиотека, не сервис) для beautygo_backend и ai-bot-platform. AIConcierge-оркестратор, tool-definitions с anti-hallucination, multi-tenant brand voices, observability. Версия 0.8.1 (pre-1.0, API может ломаться в minor-релизах).
- **Стек:** Python ≥3.12, зависимости `openai`, `pydantic`, `asgiref`; extras `[django]`, `[tiktoken]`, `[dev]`; setuptools; ruff + mypy.
- **Ключевые модули (`src/ayla_ai_core/`):** `orchestrator.py` (AIConcierge, 11-шаговый pipeline, DI: store/context/prompt/dispatcher), `tools.py` (function-calling схемы, 5 ActionType), `tool_handlers.py` (dispatch + anti-hallucination), `context.py` (SpecialistContext generic int|UUID), `prompts.py` (два голоса: FORMULA_TELA / AYLA_MARKETPLACE), `composer.py`, `memory.py` (**новое в ветке**: build_memory_block с confidence-порогами), `observability.py` (tenant-context, replay-determinism), `providers/` (OpenAI passthrough, Anthropic adapter).
- **Потребители:** djangoproject (ORM ConversationStore, AYLA_MARKETPLACE_VOICE) и ai-bot-platform — оба пинят по SHA (сейчас `d72a5de4`, выровнены DRF-1441).
- **Тесты:** 12 файлов, ~227 тест-функций, включая snapshot публичного API (`test_public_api_surface.py`) как drift-гейт.
- **Доки:** `README.md`, `CHANGELOG.md`, `RELEASING.md` (SHA-pin discipline), `LTS_POLICY.md`, ADR-0009 (в `main`).
- **Состояние:** ветка `feat/memory-context-builder` добавляет `memory.py`, но **фича не завершена**: `build_memory_block` не экспортирован из `__init__.py` (до потребителей не доходит), `[Unreleased]` в CHANGELOG пуст. Отстаёт от `main` на 2 коммита (доки). Незакоммичено: удалён `uv.lock`. Осознанный техдолг: deprecated-алиасы (удаление в v0.9.0).

### 3.4 frontAyla (frontbeauty)

- **Назначение:** мобильный фронтенд: монорепо с `apps/client` («BeautyGO», com.beautygo.client), `apps/pro` («BeautyGO Pro», com.beautygo.pro) и общим `packages/shared` (@beautygo/shared — весь API-слой, auth-store, storage).
- **Стек:** TypeScript, React 19, React Native 0.81, Expo SDK 54, expo-router 6 (file-based), axios, SecureStore, expo-auth-session; yarn workspaces. Тесты: jest + jest-expo + msw, E2E — Maestro.
- **Ключевые модули:** `shared/src/api/client.ts` (axios, Bearer JWT > anonymous JWT, X-App-Type, X-Device-Id, refresh-очередь), `auth.ts` (OTP, регистрация), `anonymousAuth.ts`, `socialAuth.ts` (VK/Google/Apple/Yandex), `masters.ts`, `bookings.ts`, `services.ts`, `auth/authStore.tsx`. Экраны: client (20 файлов, booking-flow, auth-flow), pro (14 файлов).
- **Интеграция:** только backend API; `BASE_URL` **захардкожен** `https://dev.gobeauty.site/api/v1` в `client.ts:4` — нет env-переключения prod/dev.
- **Запуск:** `yarn client` / `yarn pro` (Expo Go / prebuild); нативные `ios/`, `android/` не коммитятся.
- **Тесты:** client — 13 jest-файлов (контрактные + скрины, coverage ≥60%) + Maestro-флоу; **pro без тестов**. CI: `test.yml`, `smoke.yml`.
- **Доки:** только корневой `README.md` (команды + матрица покрытия экранов DRF-24…187). Нет `.env.example`.
- **Состояние:** последняя активность 2026-04-09 (~3 месяца затишья); `dev` на 31 коммит впереди `main`, релизный мердж не выполнен. Рабочее дерево чистое.

---

## 4. Активный трек разработки: Memory Foundation

Три репо синхронно ведут фичу «персональная память AI-консьержа» (после pivot 2026-07-09):

| Репо | Ветка | Что сделано | Что осталось |
|---|---|---|---|
| ayla-ai-core | `feat/memory-context-builder` | `build_memory_block` (рендер зелёной зоны памяти, confidence-пороги) | Экспорт из `__init__`, snapshot-тест, CHANGELOG, rebase на main, релиз + парный бамп SHA в обоих backend |
| beautygo_backend | `feat/memory-foundation-internal-api` | Internal API personal-context (GET/PATCH + ask-eligibility), frozen contract v1.0 | A1b: consent backstop, MemoryFact, confidence; rebase на dev (9 коммитов YClients) |
| ai-bot-platform | `feat/memory-consent-global` | Глобальный memory-consent (green/yellow/red + withdraw cascade) | Синхронизация с origin/dev (−20), интеграция с ai-core после релиза |

Разделение ответственности: Ayla хранит только declared prefs (зелёная зона), inferred-память/шифрование — на стороне бота; consent-гейт `memory_green` enforce'ится ботом до вызова `build_memory_block`.

---

## 5. База знаний — Obsidian Vault

- **Путь:** `C:\Users\user\Documents\Obsidian Vault` (не под версионным контролем — резервное копирование открытый вопрос).
- **Роль:** курируемый слой знаний поверх Git-репо. Корневая карта — `Ayla.md`; правила базы — `Ayla Knowledge Architecture Specification v1.0` (status: **draft**, требует утверждения владельцами Product и Architecture).
- **Модель истины:** у каждого документа frontmatter-поле `source_kind`: `canonical` (редактируется в Vault) или `mirror` (источник в Git; в Vault — копия с `source` и `synced`). Все mirror-документы синхронизированы 2026-07-17 из `djangoproject/docs/`.
- **Таксономия:** `00 Foundation` · `01 Product` · `02 User Experience` · `03 AI System` · `04 Domain Models` · `05 Architecture` · `06 Safety & Governance` · `07 Business` · `08 Research` · `09 Operations` (+ планируемые `90 Sources`). Навигация — через MOC-заметки; машинно-читаемый frontmatter (note_id, status, owner, depends_on, implements, adr) подготовлен под экспорт в Dataview/LlamaIndex/Neo4j.
- **Нормативный путь продукта** (из `Ayla.md`): Constitution v2.2 → User Journey v1.0 → User Journey Specification v1.1 → Killer Scenario PRD v1.0 → MVP Roadmap 2026-07 → ADR-0009.

### 5.1 Реестр документов Vault (26 заметок)

| Документ | Раздел | Статус | source_kind | Суть |
|---|---|---|---|---|
| Ayla Constitution v2.2 | 00 Foundation | **approved** (2026-07-16, утв. основателем) | canonical | Нормативная база: права пользователя, границы платформы |
| Knowledge Architecture Spec v1.0 | 00 Foundation | **draft** | canonical | Правила базы знаний, типы, статус-модель, frontmatter |
| Killer Scenario PRD v1.0 | 01 Product | **draft** (2026-04-13) | mirror ← `djangoproject/docs/PRD_Ayla_Killer_Scenario_v1.0.md` | PRD killer-сценария (еда→beauty), North Star метрики |
| MVP Roadmap 2026-07 | 01 Product | draft | mirror ← `djangoproject/docs/MVP_ROADMAP_2026-07.md` | Этапность, статус функций × слоёв, потоки S1–S7 |
| User Journey v1.0 | 02 UX | approved (2026-07-17) | canonical | Experience Charter: 8 стадий, 6 принципов |
| User Journey Specification v1.1 | 02 UX | approved-with-amendments (2026-07-17) | mirror ← `djangoproject/docs/product/user-journeys/…` | State machine S0–S8, метрики, Provider Trust Model |
| Ayla Design System v0.1 | 02 UX | draft | mirror ← `djangoproject/DESIGN.md` | Токены, типографика, brand voice, AI-slop blacklist |
| Personal Context Internal API Contract | 03 AI System | **accepted, FROZEN v1.0** | mirror ← `djangoproject/docs/PERSONAL_CONTEXT_INTERNAL_API_CONTRACT.md` | Declared-prefs API для бота (M-B1) |
| ADR-0009 Split Domain Architecture | 05 Architecture | accepted v1.0 (2026-05-20) | mirror ← ai-bot-platform repo (canonical) | Разделение доменов Ayla ↔ bot-platform |
| Architecture Review | 05 Architecture | draft (2026-04-23, read-only) | mirror ← `djangoproject/docs/ARCHITECTURE_REVIEW.md` | 3-частный аудит, «Minimum Lovable Ayla» |
| Cross-Domain Safety Contract | 06 Safety | reference v1.0 | mirror ← `djangoproject/docs/safety/…` | 7 секций safety-гейтов для cross-domain правил |
| Product Audit 2026-04 | 07 Business | reference v1.0 (2026-04-27) | mirror ← `djangoproject/docs/PRODUCT_AUDIT_2026-04.md` | PM-аудит: рынок, конкуренты, pricing, pre-mortem |
| Product Research Synthesis 2026-07 | 08 Research | reference | mirror ← `djangoproject/docs/research/00-SYNTHESIS.md` | Синтез 6 исследований: вердикт, риски, действия |
| Hypothesis Validation Plan 2026-04 | 08 Research | reference v1.0 (2026-04-27) | mirror ← `djangoproject/docs/HYPOTHESIS_VALIDATION_PLAN_2026-04.md` | План валидации H1/H3/H5 (сроки прошли, итоги не зафиксированы) |
| MOC × 9 + Ayla.md | все разделы | active | canonical | Навигационные карты |

**Planned-узлы (заявлены, но не написаны — НЕ нормативны):** Intent Model Spec, Recommendation Engine Spec, AI Orchestrator Spec, Dynamic User Model / Memory Lifecycle, AI Evaluation Framework, Prompt/Agent Architecture, 8 Domain Model документов, Business Model / Unit Economics, Privacy & Retention Policy, Glossary.

**Гигиена Vault:** дубликат `Ayla Knowledge Architecture Specification v1.0 1.md`; пустая `Без названия.md`; расхождение нумерации с djangoproject — `09 Operations` в Vault vs `10 Operations` в untracked docs/.

### 5.2 Стратегический контекст из актуальных документов (прочитано 2026-07-18)

- **Этапность MVP:** Этап 1 (пилот) = **MAX-бот + MAX Mini App** (`ai-bot-platform/apps/miniapp`, React/Vite, 49 экранов на 3 персоны) — канал зафиксирован решением D4 (2026-07-18); в MVP Roadmap 2026-07 был Telegram — документ устарел в этой части. Этап 2 = мобильные приложения frontAyla + AI Avatar (свинг). → frontAyla **не стагнирует, а планово отложен**.
- **Дата пилота:** ориентир был 2026-07-15 — **уже прошёл**, актуальность не подтверждена (открытый вопрос №1 roadmap); Research Synthesis упоминает 15.08.
- **Критический путь пилота:** S1 Каталог→Бронь (снятие гейта `BOOKING_VIA_AYLA_REST`, #1034) → S2 AI Concierge wiring (DRF-241) → S3 Память (~60-70% готова по коду) → S4 Food/Wellness → S5 152-ФЗ/легал (export/delete НЕ подключены — **юр-риск пилота**) → S6 платежи/уведомления e2e → S7 стабилизация.
- **Разделение памяти после pivot 2026-07-09:** Ayla хранит только declared prefs (зелёная зона, frozen contract v1.0, commit `63af097e`); inferred-память/шифрование/red-log — на стороне бота (ADR-0011/0006). Consent-гейт `memory_green` enforce'ится ботом ДО вызова; Ayla-side backstop = A1b (не сделан).
- **Research Synthesis (вердикт):** продукт технически ~90% готов, но ров «AI, который помнит» не построен и технически не защищаем; угроза Яндекс+DIKIDI+Алиса — окно 12–18 мес; **CONDITIONAL NO-GO** на заявленный scope пилота → сузить обещание. Пропущен Decision Day 2026-05-13 для H1/H3. Двойная монетизация (690₽/мес + 100₽/запись) не валидирована.
- **Safety:** 3/5 cross-domain seed-правил **FAIL** контракт (медицинские термины, shame-компаративы, отсутствие `pregnant` в exclusions) — заблокированы `clean()`-гейтом до правок legal.

### 5.3 Дайджест крупных документов (прочитаны полностью 2026-07-18)

**Ayla Constitution v2.2** (approved, 2026-07-16)
- Позиционирование: персональный AI для заботы о себе — НЕ маркетплейс/CRM/агрегатор/трекер; новый домен = отдельное архитектурное решение + утверждение основателя.
- Пользователь **не платит** за доступ (Ст. IV); провайдер платит, но не владеет пользователем/памятью/выбором; деньги не покупают приоритет в рекомендациях (экономическая нейтральность на всех этапах).
- Ст. V федеративная модель пользователя (5 типов источников, гипотезы ≠ факты); Ст. VI progressive profiling; Ст. VII право на объяснение/вето/исправление; Ст. VIII/XII архетип Хранителя (помочь/уточнить/предупредить/отказать); Ст. IX TTL знаний + «удалить всё»; Ст. X Readiness («иногда лучшая помощь — ничего не предлагать»).
- Изменения только с founder approval + заключения arch/legal/security/safety; трассируемость ADR/API/тестов к статьям. Реализуется через ADR-0011 (privacy), ADR-0012 (memory lifecycle).

**Killer Scenario PRD v1.0** (draft, 2026-04-13)
- Сценарий S1–S8: фото завтрака → детект дефицита витамина D (5+ дней) → cross-domain инсайт → top-1 рекомендация «массаж у любимого мастера, завтра 19:00» → запись в 2 тапа → аватар → прогресс → шеринг.
- Ключевой пробел: шаги S2/S3/S7 (Memory + Insight Engine) — **«Phase 1.5», никем не запланированные**; поставка: v0.1 Half Killer (S1–S5, без аватара) → v0.2 Full.
- North Star: ≥25% активных с ≥1 killer moment/нед (60 дн), ≥40% (90 дн); <15% к 60-му дню = стратегическая переоценка. CTR инсайта ≥18%, конверсия инсайт→запись ≥8%, записей по инициативе Ayla ≥20%.
- Launch-blocking риски: мед-диагноз формулировки, 152-ФЗ, supply-аудит Пензы (≥3 мастера на правило), cold-start ≥7 дней.

**User Journey v1.0** (approved, 2026-07-17)
- «Превращает неопределённость в безопасный и понятный следующий шаг»: Поняла → Выбрала → Организовала. 8 стадий S0–S7, 6 принципов (Intent before Query; Understanding before Recommendation; Recommendation before Transaction; Human Agency; Helpful Restraint; Transparent Memory).
- Точки входа: личная потребность / рекомендация человека / QR в салоне. Первый контакт — «Что привело тебя?»; запрет анкеты и продажи. Anti-patterns: каталог вместо помощи, продажа без понимания, всезнание, давление, скрытая память.

**User Journey Specification v1.1** (approved-with-amendments, 2026-07-17)
- 9 состояний S0–S8; S8 Boundary Handling — защитное, из любого состояния (мед-запрос, РПП, лекарства, острая боль, вне домена), **Safety Rate цель 100%**; нормативная state machine в YAML + 8 инвариантов.
- Каналы: **MAX Bot (primary) + Mini App (secondary)** — ⚠️ расходится с MVP Roadmap («Telegram-бот + Mini App») — открытый вопрос канала пилота.
- Context Sufficiency Gate (≥3 из 4 слотов, ≤5 вопросов/сессия); Dynamic Intent Transition (смена намерения → новый safety-оценка); Readiness Gate (explicit_do_not_disturb, topic_cooldown, quiet_hours, safety_risk и др.); Provider Trust Model (комиссия/тариф/бюджет НЕ входят в match score; запрет «Анна лучше Марии»); Concierge Mode для первых 100–500 пользователей (ручная проверка рекомендаций).
- Метрики: Intent Resolution ≥60% (провал <50%), Time-to-First-Value <5 мин, Trust ≥4.0, Booking Success ≥90%, D7 return ≥30%, D30 ≥20%; business-метрики (GMV) **запрещены** как цель оптимизации ranking.

**ADR-0009 Split Domain Architecture** (accepted, 2026-05-20; canonical — в ai-bot-platform)
- Один домен = один canonical owner; межсервисное состояние — только через event contract (envelope ULID/versioning/идемпотентные консьюмеры/Postgres outbox/12 событий MVP/старые версии ≥30 дн).
- ai-bot-platform = **AI backbone** (BotUser, память, conversations, KB/RAG, каналы, Mini App, consent, eventbus) — НЕ владеет canonical User/PII, никогда не SoR бронирований. djangoproject = **transaction backend** (User+PII, booking DDD, payments, canonical catalog). ayla-ai-core = чистая библиотека. mysite = только Формула тела.
- Booking SoR: YClients-салоны → YClients (Ayla mirror); соло-мастера → Ayla local. 7 hard rules: нет дублирующего canonical state, нет cross-repo DB, фриз MVP-фич, JWT tenant_id checks, event_version обязателен и др.
- Отвергнуто: Variant B (всё в bot-platform — срыв пилота), Variant C (всё в Ayla — 10–20K LOC, 6–12 нед), standalone memory-service (до Phase 2+).

**Architecture Review** (draft, 2026-04-23 — до-пивотный по каналам, mobile-first)
- 3-частный аудит; CEO выбрал «Minimum Lovable Ayla» (KEEP 3 innovation tokens из 8); пилот сдвинут 2026-06-30 → 2026-07-15.
- 3 silent failure modes: мёртвый outbox worker без Sentry; SQLite `select_for_update` no-op → ложнозелёные concurrency-тесты; plaintext-токены в `.mcp.json`.
- Решения: foundation-first (Postgres dev, Celery/Redis, Sentry, тест-инструментарий) → LLM benchmark (200 ru-промптов, 5 кандидатов) → AI-код. Большинство пробелов с тех пор закрыто (Celery, notifications/, nutrition/, AI app существуют).

**Product Audit 2026-04** (reference, 2026-04-27)
- Personalization Engine — load-bearing → перенесён в M3 P0 (DRF-230); beachhead сужен до «Анна 25–35, Пенза»; supply персона Олеся требует export client list как first-class фичу.
- **MAX-бот = главный канал и moat** (0 friction install, CAC −5–10x, окно 6–12 мес, конкурентов в MAX нет); worst friction — YooKassa вне MAX.
- Рынок: TAM ₽800–900 млрд/год, SOM Year 2 ₽160M ARR; главная угроза — DIKIDI/Яндекс; сильнейший «конкурент» — status quo (Instagram/WhatsApp, 50–70% записей).
- **Pricing: 8% слишком агрессивно** → рекомендация tiered 3% (3 мес) → 5% → 6–7% при density ≥150 мастеров; решение о смене НЕ зафиксировано.
- Pre-mortem elephants: consumer growth competency gap (#1 риск), data ownership с владельцем Формулы тела (договор до DRF-243), founder burnout. Реальный moat — proprietary data loop (food scan → booking → personalization).

**Hypothesis Validation Plan 2026-04** (reference, 2026-04-27)
- 3 дешёвых теста H1/H3/H5 (₽19–35k, 2 недели) с порогами и decision tree на 2026-05-13.
- **Сроки прошли, результаты не зафиксированы.** Research Synthesis подтверждает: Decision Day пропущен, Food Scanner построен как P0 без валидации H1 — продуктовый риск «строим на непроверенной гипотезе».

**Сквозные противоречия (4 из 6 решены 2026-07-18 — см. Decision Log §5.4):**
1. **UserPersonalContext:** CEO-review (04-23) откладывал в Phase 6 → Audit (04-27) требовал M3 P0 → Constitution/Spec (07) опираются как на данность; код (feat/memory-*) подтверждает memory-first. Снято практикой, но не документально.
2. ~~**Канал пилота**~~ → ✅ **D4:** MAX-бот + Mini App; Telegram вне пилотного scope.
3. ~~**Pricing**~~ → ✅ **D1:** подписка 690/990₽ + 90₽/запись, платит специалист; 8% split YooKassa уходит.
4. ~~**Монетизация пользователя**~~ → ✅ **D1:** подтверждено — пользователь не платит (Конституция Ст. IV); модель полностью на стороне специалиста.
5. ~~**Дата пилота**~~ → ✅ **D3:** 2026-08-15.
6. **Killer PRD — Draft**, но цитируется как основа Phase 1.5 и нормативный путь; Insight Engine rules-based (PRD) vs полноценный Intent/Recommendation Engine (Spec v1.1) — разные уровни зрелости, конфликт scope. → в работе по D2 (PRD v1.1 вокруг памяти).

### 5.4 Decision Log (решения владельца)

**Канонический источник (с 2026-07-18):** `ayla-knowledge` → `02 Strategy/Ayla Decision Log.md` (node_id `ayla.strategy.decision-log`, ветка `agent/decision-log` в review, далее merge в main). Глобальные ID `AYLA-DEC-0001…0010`, алиасы D1–D9 сохранены. Редактируется только там (ветка `agent/decision-log-*`, валидация, review). Ниже — краткое зеркало, **не редактировать здесь**.

- AYLA-DEC-0001 (D1): pricing 690/990₽/мес + 90₽/запись — платит специалист; пользователь не платит.
- AYLA-DEC-0002 (D2): moat = память/«расширенное понимание»; еда — триггер, не ядро.
- AYLA-DEC-0003 (D3): пилот **2026-08-15**, Пенза.
- AYLA-DEC-0004 (D4): канал — MAX-бот + MAX Mini App; Telegram вне scope.
- AYLA-DEC-0005 (D5): статусы документов — pending decision.
- AYLA-DEC-0006 (D6): онлайн-оплата клиентом опциональна (пересмотрено в тот же день; superseded-редакция — в каноне).
- AYLA-DEC-0008 (D8): split per-master YooKassa; кошелёк — этап 2. ⚠️ утверждение про 161-ФЗ — рабочая архитектурная гипотеза, требует подтверждения юриста до перевода документа в approved.
- AYLA-DEC-0009 (D9): capture-стратегия (`CAPTURE_DELAY_HOURS=0`, параметризуемо) — ADR `docs/architecture/payments-capture-strategy.md`.
- AYLA-DEC-0007 (D7): сбор с мастера — автосписание (рекуррент).
- AYLA-DEC-0010: **инвариант одиночного взыскания fee** — online-paid → 90₽ через split; offline-paid → 90₽ в billing; одна completed booking → ровно одно взыскание.

## 6. Расхождения с исходным описанием и риски

1. **«Актуальный код в dev» — не подтверждено.** Три из четырёх рабочих копий на feature-ветках Memory Foundation; актуальная работа идёт там, dev/main отстают.
2. ~~**frontAyla — 4-й репозиторий**~~ → **ПОДТВЕРЖДЕНО 2026-07-18:** `Shiro-Py/frontbeauty` — репозиторий проекта (отдельный GitHub-аккаунт), владение подтверждено владельцем. Осталось: мердж dev→main (+31 коммит) при подходе к этапу 2.
3. **ayla-ai-core: основная ветка — `main`, не `master`.**
4. **Локальные копии рассинхронизированы с origin:** ai-bot-platform −20 от origin/dev; ayla-ai-core −2 от main; djangoproject −9 от dev (пропущен YClients intake — риск конфликтов при rebase).
5. **Незакоммиченная работа:** реструктуризация `docs/` в djangoproject (15 untracked путей), ~60 untracked в ai-bot-platform, удалённый `uv.lock` в ayla-ai-core.
6. **frontAyla отложен до этапа 2** (по MVP Roadmap — планово, не стагнация), но: BASE_URL захардкожен, pro-приложение без тестов, релизный мердж dev→main не выполнен (+31 коммит).
7. **Кросс-сервисная доставка событий backend→бот выключена** (`OUTBOX_EXTERNAL_DELIVERY_TOPICS` пуст по умолчанию) — round-trip не подтверждён.
8. **Мультитенантность в permissive-режиме** в обоих backend (strict-flip отложен).
9. ~~**Дата пилота прошла**~~ → **РЕШЕНО 2026-07-18 (D3):** пилот — **2026-08-15**. Риск смещается в исполнение: ровно 4 недели на критпуть S1 (каталог→бронь) + S3 (память) + легал (152-ФЗ, 3/5 cross-domain правил).
10. ~~**Vault не под версионным контролем**~~ → **РЕШЕНО 2026-07-18:** каноническая база знаний переехала в Git-репо `ayla-knowledge` (валидатор + review-gate + unittests); старый vault в `Documents/Obsidian Vault` мигрирует по одному документу (процесс миграции — Knowledge Architecture Spec §13).
11. **Юридический риск пилота:** 152-ФЗ export/delete в Mini App не подключены (Variant 3 stub), 3/5 cross-domain правил заблокированы до правок legal.
12. ~~**Канал пилота не определён**~~ → **РЕШЕНО 2026-07-18 (D4):** основной канал — **MAX-бот + MAX Mini App** (совпадает с Journey Spec v1.1 и Product Audit). Telegram — вне пилотного scope. Требуется правка MVP Roadmap (там устаревший Telegram). Известный риск: фрикция оплаты YooKassa вне MAX (нужен Mini App SDK) — по Product Audit.
13. ~~**Pricing-конфликт**~~ → **РЕШЕНО 2026-07-18 (D1):** подписка 690₽/мес (соло), 990₽/мес (салон, 3 мастера) + 90₽ за успешную запись; платит специалист. Соответствует Конституции Ст. IV (пользователь не платит). Остаточные риски: flat 90₽ регрессивен на дешёвых услугах (18% при чеке 500₽); подписочного биллинга в коде нет — новый эпик; 8% split YooKassa в `payments/` подлежит пересмотру.
14. **H1 не валидирована:** Decision Day 2026-05-13 пропущен, Food Scanner построен как P0 на непроверенной гипотезе daily-hook.

## 7. Пробелы данных (что индекс не покрывает)

- Состояние удалённых веток `origin/dev`/`origin/main` (локальные refs могут быть старыми; нужен `git fetch` для точного diff).
- Прод-инфраструктура: VPS, nginx, systemd, секреты (видны только конфиги, не состояние).
- CI/CD пайплайны в GitHub Actions — видны файлы workflow, не история прогонов.
- Трекер задач (Linear, DRF-###) — видны только ссылки из кода/доков.
- Результаты Hypothesis-тестов H1/H3/H5 (апрель–май 2026) — в документах не зафиксированы; Decision Day пропущен.
