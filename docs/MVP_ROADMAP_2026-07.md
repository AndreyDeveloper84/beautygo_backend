# Ayla — Роадмап до MVP (2026-07)

> DRAFT. Составлен 2026-07-01 по фактическому состоянию кода (не по устаревшим докам).
> Решения владельца зафиксированы в memory `project_mvp_staging_bots_miniapp`.

> **REBASED 2026-07-19 (W6)** под Decision Log v0.2 (`ayla-knowledge/02 Strategy/Ayla Decision Log.md`, AYLA-DEC-0001…0010) и Pilot Contracts v1.3.0.
> **Действующий план пилота — `docs/PILOT_STREAMS_2026-08-15.md`** (потоки W1–W6, недели 18.07–15.08);
> межпоточные контракты — `docs/PILOT_CONTRACTS_2026-08-15.md`; статус стыков — `docs/qa/CONTRACT_MATRIX_2026-08-15.md`.
> Этот документ — историческая база + статусная сверка. Superseded-фрагменты помечены
> `[SUPERSEDED → AYLA-DEC-XXXX]` и **сохранены для истории** (не удаляются). Лог ребейза — §7.

## 0. Этапность (ключевое)

- **Этап 1 (пилот MVP) = бот + Mini App.** Канал пилота — **MAX-бот + MAX Mini App** (AYLA-DEC-0004).
  `[SUPERSEDED → AYLA-DEC-0004]` «Telegram-бот + Mini App» — Telegram вне пилотного scope.
  **Дата пилота: 2026-08-15, Пенза** (AYLA-DEC-0003; прежние даты 2026-06-30 и 2026-07-15 отменены).
- **Этап 2 = мобильные приложения** (React Native `frontAyla`, уже сильно готовы на origin/dev) **+ AI Avatar**.
- **В MVP входят ВСЕ функции.** **AI Avatar — свинг:** оцениваем сложность/сроки; не влезаем → этап 2. (Открытый вопрос §6.4.)
- **Модель монетизации пилота (AYLA-DEC-0001):** подписка 690₽/мес (соло) / 990₽/мес (салон) + 90₽ за успешную запись — платит **специалист**; пользователь не платит (Constitution ст. IV).
  `[SUPERSEDED → AYLA-DEC-0001]` 8% split в коде и tiered-модель из Product Audit.
- **Онлайн-оплата клиентом — опциональна** (AYLA-DEC-0006): путь записи без предоплаты сохраняется; capture — немедленный после complete, `CAPTURE_DELAY_HOURS=0` (AYLA-DEC-0009, ADR `docs/architecture/payments-capture-strategy.md`); расчёты с мастером — **YooKassa split per-master** (AYLA-DEC-0008), внутренний баланс/кошелёк — этап 2; сбор с мастера — **автосписание (рекуррент)** (AYLA-DEC-0007).

## 1. Архитектура поверхности этапа 1

```
Пользователь → MAX-бот ─────┐   [SUPERSEDED → AYLA-DEC-0004]: «Telegram Bot»
             → MAX Mini App ┤
                             ▼
   ┌───────────────── ai-bot-platform ─────────────────┐
   │ FE:  apps/miniapp (Vite/TS)                         │
   │ BE:  miniapp_api, ingress, channels, orchestrator,  │
   │      skills (booking, food_scanner, ...), llm,      │
   │      persona, catalog (mirror), integrations/ayla   │
   └───────────────┬──────────────────────┬─────────────┘
                   │ REST (booking/slots/  │ shared pkg
                   │ catalog)              ▼
                   ▼               ┌───────────────┐
          ┌─────────────────┐      │  ayla-ai-core │ (orchestrator,
          │  Ayla backend   │      │  tools, context│  persona, tools)
          │  (djangoproject)│      └───────────────┘
          └─────────────────┘
```

**4 слоя задач:** **A** = Ayla backend · **B** = Bot backend · **M** = Mini App FE · **C** = ayla-ai-core.

## 2. Функции MVP × статус по слоям

Легенда: ✅ готово · 🟡 частично/доводка · ❌ нет · — не применимо · «verify» = требует точной сверки лидом стрима.

> Статусы строк 5, 6, 13, 14 обновлены 2026-07-19 (W6) по факту кода; доказательства — `docs/qa/CONTRACT_MATRIX_2026-08-15.md`. Добавлена строка 6а (Billing — новый эпик AYLA-DEC-0001/0007, в исходной редакции отсутствовал).

| # | Функция | A (Ayla) | B (Bot) | M (MiniApp) | C (ai-core) | Критпуть |
|---|---------|----------|---------|-------------|-------------|:--:|
| 1 | Auth / identity | ✅ OTP/anon/social | ✅ identity/tenancy | ✅ TMA auth (miniapp_api) | — | |
| 2 | **Канонический каталог услуг** | ✅ сид + internal API (W1) | ✅ link-команда (покрытие перед флипом) | 🟡 verify | — | 🔴 |
| 3 | Каталог мастеров + профиль | ✅ specialists | ✅ CatalogMaster mirror | 🟡 verify | — | |
| 4 | Расписание + слоты | ✅ schedule/slots | 🟡 scheduling | 🟡 slot picker | — | |
| 5 | **Бронирование** | ✅ booking engine + REST (#1016, `payment_required` AMD-002) | ✅ Ayla REST proxy (гейт OFF до флипа) | ✅ booking flow реальный (p4) | 🟡 booking tools | 🔴 |
| 6 | Оплата клиентом (YooKassa, **опционально** — AYLA-DEC-0006) | ✅ hold→capture на complete + flat 90₽ + split per-master + auto-cancel холда (W1, AYLA-DEC-0008/0009) | 🟡 linkage verify | 🟡 payment screen | — | |
| 6а | **Billing мастера** (подписка 690/990 + 90₽/запись — AYLA-DEC-0001/0007) | 🟡 W2 `billing/`: модели+C1/C2/C5 готовы (не смёржено); рекуррент/dunning — волна 2 | 🟡 proxy C2/C3 готов (mapping gap 503) | ❌ экран биллинга + «К выплате» + consent | — | 🔴 |
| 7 | Отзывы | ✅ | verify | 🟡 review screen | — | |
| 8 | Уведомления (push/SMS/DM) | ✅ notifications | ✅ bot DM + R1 T−24h (W3) | verify | — | |
| 9 | Избранное | ✅ favorites | verify | 🟡 verify | — | |
| 10 | **AI Concierge / Chat** | ✅ ai app | 🟡 orchestrator/llm | 🟡 chat UI | ✅ v0.9.0 memory block; 🟡 wiring DRF-241 | 🟠 |
| 11 | **Food Scanner** (daily-hook) | ✅ nutrition | 🟡 skills/food_scanner (полиш) | 🟡 food screens | — | 🟠 |
| 12 | Wellness / «День» | verify | 🟡 | 🟡 day tab (#951) | — | |
| 13 | **UserPersonalContext (Память)** — **центр killer-сценария (AYLA-DEC-0002)** | ✅ green-зона + internal API v1.0 (A1a) | ✅ persistence + consent cascade | 🟡 surfacing | ✅ `build_memory_block` v0.9.0; ❌ should_ask wiring в консьерже (W5) | 🔴 |
| 14 | Profile / consent / 152-ФЗ | 🟡 export/delete готовы на ветке W2 (не смёржено) | ✅ consent + C5 bot endpoints | 🟡 profile (#946-953) + шторки WIP | — | 🔴 юр |
| 15 | AI Avatar (свинг) | ❌ verify | — | 🟡 (в мобиле есть) | — | ⏸ этап2? |

## 2.1 Глубокая сверка Mini App (`apps/miniapp`, 2026-07-02)

> Историческая сверка на 2026-07-02; актуальный статус W4 — `docs/qa/TEST_SPEC_W4.md` и CONTRACT_MATRIX. Изменения 2026-07-19 (W6): канал — MAX; booking-flow раз-stub'лен (п.1 закрыт на ветке `pilot/miniapp`); фронт-тесты появились (п.4, 4 файла vitest).

Стек: React + react-router-dom + Vite/TS. **49 экранов на 3 персоны** (Customer /
Master / Admin), 15 API-модулей в `src/lib`. **typecheck чистый** (`tsc --noEmit`).
Активная разработка («Tier 1 Priority N Phase B» треки, свежие коммиты).

**Готово (wired на реальные эндпоинты бот-бэка `/api/v1/customer|master|admin|me`):**
- Customer: каталог, деталь мастера, слоты, booking-flow (F1-F5), мои записи, деталь/перенос, records, wellness-дашборд (7 блоков), профиль, отзывы.
- Food Scanner: 6 экранов (capture/processing/result/saved/diary/manual) + EXIF-strip + consent.
- Master: dashboard, расписание, услуги, клиенты, профиль, настройки, онбординг, conversations/internal-chat.
- Admin: команда, инвайт мастера, деактивация, services-matrix, availability-requests, internal-chat.

**Важно — фронт зовёт БОТ-бэк, а не Ayla напрямую.** MiniApp → bot API → (Ayla REST).
Значит «мост в Ayla» сидит в бот-бэке (подтверждает критпуть S1), фронт менять не надо.

**Конкретные дырки под пилот (на 2026-07-02; статусы — в CONTRACT_MATRIX):**
1. 🔴 **Booking-flow построен «against stub»** (#856) — UI готов, но реальное заземление
   на Ayla REST не завершено. Это НЕ FE-задача — это S1 (каталог+бронь через Ayla).
   → **2026-07-19: закрыто на `pilot/miniapp`** (реальные слоты/create/cancel/reschedule).
2. 🟡 **Customer profile — deferred для пилота** (Variant 3 stub): R2-шторки
   **export + delete (152-ФЗ права)** НЕ подключены, support deeplink — placeholder. **Юр-риск пилота.**
   → **2026-07-19: WIP** — шторки есть, прямой API-wiring в работе (test-first, W4).
3. 🟡 **Food scanner consent persistence** (`food_scanner_consent_at`) отложен в W4.
4. 🟡 **0 фронт-тестов** в miniapp — риск качества под пилот.
   → **2026-07-19: 4 файла vitest + WIP personal-data** (W4).
5. **Нет отдельного экрана памяти/AI-чата у клиента** — клиентский диалог = **MAX-бот DM**
   `[SUPERSEDED → AYLA-DEC-0004]: «Telegram-бот DM»`
   (не miniapp), а surfacing персонализации живёт в wellness/recommendations. Память (S3)
   подключается в эти точки, новый экран не обязателен.
6. Второстепенное: часть admin-настроек «скоро», M8 minimal.

**Вывод:** Mini App FE **в основном готов и качественен**; критичное — не «дописать экраны»,
а (S1) заземлить booking на Ayla и (легал) подключить 152-ФЗ export/delete + consent-persist,
и (S7) добавить фронт-тесты. Основной объём «дописывания UI» — НЕ здесь.
→ **2026-07-19:** добавились экран биллинга мастера, «К выплате», consent автоплатежа (W4, AYLA-DEC-0001/0007).

## 3. Критический путь этапа 1

> Актуальный критический путь (PILOT_STREAMS, 2026-07-18): sync веток → S1 (каталог + booking REST без оплаты) → флип гейта → un-stub miniapp → e2e. **Параллельно: billing (W2) и memory (W5) — закрыть к неделе 3 (01–08.08).** Заморозка фич 12.08, пилот 15.08.

1. **Канонический каталог → бот-бронь** (🔴): PR #201 (Ayla сид) → миграция/наполнение → **link `ayla_service_id`** в боте → покрытие ≥ порог → снять гейт `BOOKING_VIA_AYLA_REST` (#1034) → бот пишет бронь в Ayla. Это сердце пилота (бот=канал).
2. **AI Concierge wiring** (🟠): ayla-ai-core → orchestrator/persona в боте (DRF-241), чтобы диалог=подбор+бронь работал сквозно.
3. **UserPersonalContext / Память** (🔴 build): **центр killer-сценария (AYLA-DEC-0002)** — «Ayla помнит и понимает меня»; цепочка еда→beauty остаётся триггером, но не ядром. BE (Ayla) ✅ + persistence (bot) ✅ + context (ai-core v0.9.0) ✅ + **should_ask wiring (W5) — главный остаток**.
4. **Billing (денежный контур мастера)** (🔴, добавлено ребейзом — AYLA-DEC-0001/0007/0008/0010): привязка карты → рекуррент (подписка + 90₽/запись) → dunning → блокировка записей (C1); инвариант одиночного взыскания fee.
5. **Food Scanner полиш** (🟠): закрыть хвост issue (#956-994) + wellness/«День».
6. **Стабилизация/безопасность/QA** под пилот (ADR-0009 хвост, e2e оплаты/уведомлений; канарейка + runbook — W6).

## 4. Раскладка задач FE ↔ BE (для старта)

> Действующая раскладка — брифы W1–W6 в `docs/PILOT_STREAMS_2026-08-15.md`; ниже — историческая (2026-07-01) с пометками ребейза.

### BE — Ayla (`djangoproject`)
- Каталог: домёржить #201; наполнить `requires_health_check`/длительности пилотных услуг; internal API отдаёт `template_id`/`slug` боту (#200 t5). → **2026-07-19: ✅ (W1)**
- **UserPersonalContext API** (модель + CRUD + 152-ФЗ зоны) — новый крупный блок. → **2026-07-19: ✅ green-зона + internal API v1.0; export/delete (C5) — на ветке W2.**
- Booking-via-REST: подтвердить контракт слотов/каталога/брони для бота (#1016). → **2026-07-19: ✅ (W1)**
- Оплата e2e (hold→capture на completed), уведомления e2e. → **2026-07-19: ✅ capture-контур (W1, AYLA-DEC-0009): hold→capture, flat 90₽, split per-master, auto-cancel, reconciliation, алерты, `retry_capture`.**
- **Billing (добавлено ребейзом, W2):** модели TariffPlan/SpecialistSubscription/BookingFee/BillingInvoice/BillingPayment/BillingConsent; первый платёж (`save_payment_method`) + рекуррент; dunning → `past_due` → C1-блокировка; чеки 54-ФЗ платформа→мастер; consent автоплатежа; C2/C4/C5 контракты.
- Analytics события пилота.

### BE — Bot (`ai-bot-platform`)
- **`link_ayla_service_ids`** команда + прогон + отчёт покрытия (снятие гейта #1034). → **2026-07-19: команда ✅; прогон покрытия Пензы + отчёт — перед флипом (go/no-go).**
- Домёржить #1045 (hardening) → подготовить флип #1041 к включению по готовности каталога.
- AI orchestrator/persona ↔ ayla-ai-core wiring (booking + concierge). → **2026-07-19: ai-core v0.9.0 отращён; бамп SHA в djangoproject — открыт.**
- Food scanner: закрыть хвост (#956-994), consent/152-ФЗ (#956).
- Memory persistence (persona/context) на стороне бота. → **2026-07-19: ✅ + consent cascade (W3).**
- **Billing proxies (добавлено ребейзом, W3):** C2/C3 в master_api ✅ (fail-closed 503 до specialist mapping); billing-события в ALLOWED_EVENT_NAMES ✅; consumers → уведомления мастеру — после mapping.

### FE — Mini App (`ai-bot-platform/apps/miniapp`)
- Экраны: каталог услуг/мастеров, слоты, booking-flow, оплата, отзывы. → **2026-07-19: booking-flow реальный ✅ (p4).**
- Food: scan/result/insights + wellness/«День» (фикс #951).
- Profile/consent экраны (#946-953), surfacing персонализации (Память). → **2026-07-19: 152-ФЗ шторки WIP (прямой API).**
- **Экран биллинга мастера + «К выплате» + consent автоплатежа (добавлено ребейзом, W4).**
- AI-chat UI (если в miniapp, не только в боте). → клиентский диалог = MAX-бот DM (AYLA-DEC-0004).

### Shared — ayla-ai-core
- Orchestrator/tools/persona/prompts под booking + concierge.
- **context.py → UserPersonalContext**: контракт памяти (что пишем/читаем, зоны деликатности). → **2026-07-19: `build_memory_block` v0.9.0 ✅; Memory Lifecycle spec — черновик W6 (оркестратору).**
- Обновить README (устарел: пишет 0.1.0, по факту 0.8.1). → **2026-07-19: актуально 0.9.0; README-пин — открытая мелочь (W6 baseline, находка 3).**

### Кросс-репо контракты
- Eventbus Ayla→bot (booking.*/payment.*) — уже выровнены (#946 в боте про ALLOWED_EVENT_NAMES — verify). → **2026-07-19: ✅ + billing-топики (C4); внешняя доставка Ayla-side — flip оркестратора (D-3).**
- ayla_service_id как ключ каталога (Ayla UUID) — единый. → **2026-07-19: матчинг по AMD-001.**
- **Ключ мастера во всех billing/payout стыках — Ayla User UUID (добавлено ребейзом, AMD-005).**
- **Деньги — Decimal-строки 2 знака, RUB, ROUND_HALF_UP (добавлено ребейзом, Contracts §1).**

## 5. Предложение по потокам/агентам (следующий шаг — «потом параллелим»)

> **[SUPERSEDED → AYLA-DEC-0003 + оркестратор 2026-07-18]:** потоки S1–S7 и волны §5.2 заменены действующими **W1–W6** и неделями 1–4 — `docs/PILOT_STREAMS_2026-08-15.md`. Разделы 5–5.2 сохранены как историческая декомпозиция (соответствие: S1→W1/W3, S2→W5, S3→W5+W2(C5), S4→post-pilot-полиш, S5→W4, S6→W1/W2/W3, S7→W6).

- **S1 — Catalog→Booking bridge** (A+B): каталог, link, флип-готовность. Критпуть.
- **S2 — AI Concierge** (C+B): ai-core wiring, диалог-бронь.
- **S3 — Memory / UserPersonalContext** (A+B+C): новый крупный блок.
- **S4 — Food & Wellness** (B+M): скайнер, день, полиш.
- **S5 — Mini App FE** (M): каталог/booking/profile/оплата экраны.
- **S6 — Payments & Notifications e2e** (A+B).
- **S7 — Stabilization/Security/QA** (кросс): ADR-0009, e2e, pilot-readiness.

> Детализация потоков в задачи + назначение агентов — отдельным шагом, когда согласуем этот скелет.

## 5.1 Декомпозиция потоков в задачи (2026-07-02)

Тег `[слой]`: A=Ayla · B=Bot · M=MiniApp · C=ai-core. Статус по факту кода.

### S1 — Catalog→Booking bridge 🔴 (критпуть, стартует первым)
- S1.1 `[A]` домёржить #201; наполнить пилотные услуги (health-check + длительности).
- S1.2 `[A]` internal API отдаёт `template_id`/`slug` боту (#200 t5).
- S1.3 `[B]` **`link_ayla_service_ids`** команда + прогон + отчёт покрытия (снимает гейт #1034).
- S1.4 `[B]` домёржить #1045 (hardening); подготовить флип #1041 к включению.
- S1.5 `[B]` заземлить booking-flow miniapp на Ayla REST (убрать stub #856).
- S1.6 `[A+B]` e2e-smoke miniapp→bot→Ayla: create/reschedule/cancel. → W6, волна 3 (`scripts/pilot_smoke/`).

### S2 — AI Concierge 🟠
- S2.1 `[C]` orchestrator/persona/tools под booking+recommend (сверить актуальность 0.8.1). → актуально 0.9.0 (2026-07-19).
- S2.2 `[B]` wire ayla-ai-core в бот DM (DRF-241) — диалог-подбор-бронь.
- S2.3 `[C]` inject personal-context в prompts (context_builder) ← связка с S3. → `build_memory_block` ✅ (v0.9.0).
- S2.4 `[B]` recommendations (`/catalog/recommendations` уже зовётся фронтом) — verify.
- Deps: S1 (каталог), S3 (контекст).

### S3 — Memory / UserPersonalContext 🔴 (~60-70% готово)
- ✅ уже есть: модель + green-зона, GET/PATCH (DRF-174), **8 правил движка** (personalization_engine), behavioral Celery (infer_user_patterns), события.
- S3.1 `[A]` **wire 152-ФЗ endpoints** (POST /skip/, DELETE /<field>/, DELETE /) — код в personal_context_views есть, НЕ подключён. → **2026-07-19: C5 export/delete реализованы W2 (`users/personal_data_api.py`), ждут merge.**
- S3.2 `[A]` **yellow + red зоны**: поля + at-rest шифрование + red не в GET по умолчанию + retention 90д + отдельный access-log. → объём пилота: green-зона (Memory Lifecycle spec, W6-черновик); yellow/red — post-pilot (Contracts §9), если оркестратор не решит иначе.
- S3.3 `[A]` включить Celery-beat для `infer_user_patterns` (source 2) — verify.
- S3.4 `[A/C]` source 3: structured-extraction WRITE из чата (сейчас только hint-read).
- S3.5 `[B/C]` concierge вызывает `should_ask_question` и **органично задаёт вопрос в DM** (source 1 end-to-end). → **2026-07-19: главный остаток памяти (W5).**
- S3.6 `[A]` метрики: fill/answer/usage/skip rate (события есть → дашборд).

### S4 — Food & Wellness 🟠
- S4.1 `[B]` хвост food-scanner (#956/958/959/969/970/993/994).
- S4.2 `[M]` `food_scanner_consent_at` persist (W4 follow-up).
- S4.3 `[M]` фикс «День»/wellness (#951 → HelloScreen).
- S4.4 `[A]` nutrition cross-domain (дефицит витамина → рекомендация мастера) — verify.

### S5 — MiniApp легал+хардининг 🟡 (сжат)
- S5.1 `[M]` 152-ФЗ: customer-profile export + delete шторки (→ endpoints S3.1).
- S5.2 `[M]` фронт-тесты (сейчас 0) на критичные флоу (booking/food/consent).
- S5.3 `[M]` support-deeplink (#949), notification-prefs (#948), profile-полиш (#946-953).

### S6 — Payments & Notifications e2e 🟡
- S6.1 `[A]` оплата hold→capture на completed + webhook + refund — verify e2e. → **2026-07-19: ✅ (W1, mock ЮKassa в тестах).**
- S6.2 `[A+B]` уведомления push/SMS + бот-DM на booking.*/payment.*.
- S6.3 `[A/B]` eventbus: booking.confirmed/no_show в ALLOWED_EVENT_NAMES (бот #946). → **2026-07-19: ✅ + billing-топики.**

### S7 — Stabilization / Security / QA 🟡 (кросс, непрерывно)
- S7.1 ADR-0009 хвост (#928/#968/#1001).
- S7.2 e2e пилотные сценарии (booking / food daily / memory-ask). → W6, волна 3.
- S7.3 security: token rotation Phase 0, red-zone guards (#1008).
- S7.4 pilot-readiness runbook (dual-system delete #937). → W6, волна 3.

## 5.2 Волны параллелизации

> **[SUPERSEDED → AYLA-DEC-0003]:** действующие волны — недели 1–4 (18.07–15.08) в `docs/PILOT_STREAMS_2026-08-15.md` §«Волны»; заморозка фич **12.08**, пилот **15.08**.

- **Волна 1 (сразу, параллельно):** S1 (критпуть), S3 (независим, крупнейший), S4 (полиш), S5.2 (тесты).
- **Волна 2 (после S1):** S2 (нужен каталог+бронь), S6 (нужны booking-события), раз-stub miniapp booking.
- **S7 — непрерывно** через обе волны.
- Агенты: Backend Architect (S1-A/S3-A/S6-A) · general-purpose/Django (S1-B/S4-B) · AI Engineer (S2/S3.4-3.5) · Frontend Developer (S5/S4-M) · Security Engineer (S7).

## 6. Открытые вопросы
1. Дата пилота (ориентир был 2026-07-15 — актуально?). → **Закрыто (AYLA-DEC-0003): 2026-08-15, Пенза.**
2. Порог покрытия `ayla_service_id` для снятия гейта флипа (§#1034): для пилота Пензы реально ~100%. → Подтверждено (PILOT_STREAMS W3); прогон + отчёт покрытия — go/no-go флипа.
3. Память: полный объём для этапа 1 или минимальный слой (пара полей: любимый мастер, адрес работы, бюджет)? → **Пилотный контур (2026-07-19):** green-зона + memory-ask (source 1) end-to-end + export/delete (C5); объём и TTL — Memory Lifecycle spec (черновик W6 оркестратору); yellow/red зоны — post-pilot (Contracts §9).
4. AI Avatar: финальное решение этап 1 vs 2 — по оценке сложности (S-стрим оценит). → Открыт; ориентир — этап 2 (§0), решение владельца.

## 7. Rebase log (2026-07-19, W6)

| Решение | Что изменено в документе |
|---|---|
| AYLA-DEC-0001 (pricing D1) | §0: модель монетизации; таблица §2: строка 6а (Billing); §3 п.4; §4 (A/B/M billing-задачи) |
| AYLA-DEC-0002 (память D2) | §2 строка 13; §3 п.3 — память = центр killer-сценария, еда — триггер |
| AYLA-DEC-0003 (дата D3) | §0: пилот 2026-08-15; §5/5.2 superseded-пометки (волны W1–W6); §6.1 закрыт |
| AYLA-DEC-0004 (канал D4) | §0, §1 (диаграмма), §2.1 п.5, §4 (M): Telegram → MAX, superseded-пометки |
| AYLA-DEC-0006 (онлайн-оплата D6) | §0: опциональность; §2 строка 6 |
| AYLA-DEC-0007 (автосписание D7) | §0; §3 п.4; §4 billing-задачи |
| AYLA-DEC-0008 (split per-master D8) | §0; §2 строка 6 |
| AYLA-DEC-0009 (capture D9) | §0: CAPTURE_DELAY_HOURS=0 + ADR-ссылка; §4 (S6.1 ✅) |
| AYLA-DEC-0010 + AMD-005 | §4 кросс-репо: ключ мастера User UUID; §3 п.4 инвариант fee |
| Статусная сверка W6 | §2 (строки 2,5,6,8,13,14), §2.1 (п.1,2,4), §4 — пометки «2026-07-19» со ссылкой на CONTRACT_MATRIX |

Правило ребейза: исходный текст не удалялся; устаревшие фрагменты помечены `[SUPERSEDED → …]`.
