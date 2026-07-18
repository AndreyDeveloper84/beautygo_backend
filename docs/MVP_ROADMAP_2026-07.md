# Ayla — Роадмап до MVP (2026-07)

> DRAFT. Составлен 2026-07-01 по фактическому состоянию кода (не по устаревшим докам).
> Решения владельца зафиксированы в memory `project_mvp_staging_bots_miniapp`.

## 0. Этапность (ключевое)

- **Этап 1 (пилот MVP) = боты + Mini App.** Канал пилота — Telegram-бот + Mini App.
- **Этап 2 = мобильные приложения** (React Native `frontAyla`, уже сильно готовы на origin/dev) **+ AI Avatar**.
- **В MVP входят ВСЕ функции.** **AI Avatar — свинг:** оцениваем сложность/сроки; не влезаем → этап 2.

## 1. Архитектура поверхности этапа 1

```
Пользователь → Telegram Bot ─┐
             → Mini App ──────┤
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

| # | Функция | A (Ayla) | B (Bot) | M (MiniApp) | C (ai-core) | Критпуть |
|---|---------|----------|---------|-------------|-------------|:--:|
| 1 | Auth / identity | ✅ OTP/anon/social | ✅ identity/tenancy | ✅ TMA auth (miniapp_api) | — | |
| 2 | **Канонический каталог услуг** | 🟡 сид PR #201 | ❌ ayla_service_id link | 🟡 verify | — | 🔴 |
| 3 | Каталог мастеров + профиль | ✅ specialists | ✅ CatalogMaster mirror | 🟡 verify | — | |
| 4 | Расписание + слоты | ✅ schedule/slots | 🟡 scheduling | 🟡 slot picker | — | |
| 5 | **Бронирование** | ✅ booking engine | 🟡 skill + REST-флип (гейт) | 🟡 booking flow | 🟡 booking tools | 🔴 |
| 6 | Оплата (YooKassa) | ✅ | 🟡 linkage verify | 🟡 payment screen | — | |
| 7 | Отзывы | ✅ | verify | 🟡 review screen | — | |
| 8 | Уведомления (push/SMS/DM) | ✅ notifications | 🟡 bot DM | verify | — | |
| 9 | Избранное | ✅ favorites | verify | 🟡 verify | — | |
| 10 | **AI Concierge / Chat** | ✅ ai app | 🟡 orchestrator/llm | 🟡 chat UI | 🟡 wiring DRF-241 | 🟠 |
| 11 | **Food Scanner** (daily-hook) | ✅ nutrition | 🟡 skills/food_scanner (полиш) | 🟡 food screens | — | 🟠 |
| 12 | Wellness / «День» | verify | 🟡 | 🟡 day tab (#951) | — | |
| 13 | **UserPersonalContext (Память)** | ❌ | 🟡 persona? | ❌ | 🟡 context.py | 🔴 build |
| 14 | Profile / consent / 152-ФЗ | verify | ✅ consent | 🟡 profile (#946-953) | — | |
| 15 | AI Avatar (свинг) | ❌ verify | — | 🟡 (в мобиле есть) | — | ⏸ этап2? |

## 2.1 Глубокая сверка Mini App (`apps/miniapp`, 2026-07-02)

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

**Конкретные дырки под пилот:**
1. 🔴 **Booking-flow построен «against stub»** (#856) — UI готов, но реальное заземление
   на Ayla REST не завершено. Это НЕ FE-задача — это S1 (каталог+бронь через Ayla).
2. 🟡 **Customer profile — deferred для пилота** (Variant 3 stub): R2-шторки
   **export + delete (152-ФЗ права)** НЕ подключены, support deeplink — placeholder. **Юр-риск пилота.**
3. 🟡 **Food scanner consent persistence** (`food_scanner_consent_at`) отложен в W4.
4. 🟡 **0 фронт-тестов** в miniapp — риск качества под пилот.
5. **Нет отдельного экрана памяти/AI-чата у клиента** — клиентский диалог = Telegram-бот DM
   (не miniapp), а surfacing персонализации живёт в wellness/recommendations. Память (S3)
   подключается в эти точки, новый экран не обязателен.
6. Второстепенное: часть admin-настроек «скоро», M8 minimal.

**Вывод:** Mini App FE **в основном готов и качественен**; критичное — не «дописать экраны»,
а (S1) заземлить booking на Ayla и (легал) подключить 152-ФЗ export/delete + consent-persist,
и (S7) добавить фронт-тесты. Основной объём «дописывания UI» — НЕ здесь.

## 3. Критический путь этапа 1

1. **Канонический каталог → бот-бронь** (🔴): PR #201 (Ayla сид) → миграция/наполнение → **link `ayla_service_id`** в боте → покрытие ≥ порог → снять гейт `BOOKING_VIA_AYLA_REST` (#1034) → бот пишет бронь в Ayla. Это сердце пилота (бот=канал).
2. **AI Concierge wiring** (🟠): ayla-ai-core → orchestrator/persona в боте (DRF-241), чтобы диалог=подбор+бронь работал сквозно.
3. **UserPersonalContext / Память** (🔴 build): единственная крупная нестроенная функция. North Star. Нужен BE (Ayla) + persistence (bot) + context (ai-core) + surfacing (miniapp).
4. **Food Scanner полиш** (🟠): закрыть хвост issue (#956-994) + wellness/«День».
5. **Стабилизация/безопасность/QA** под пилот (ADR-0009 хвост, e2e оплаты/уведомлений).

## 4. Раскладка задач FE ↔ BE (для старта)

### BE — Ayla (`djangoproject`)
- Каталог: домёржить #201; наполнить `requires_health_check`/длительности пилотных услуг; internal API отдаёт `template_id`/`slug` боту (#200 t5).
- **UserPersonalContext API** (модель + CRUD + 152-ФЗ зоны) — новый крупный блок.
- Booking-via-REST: подтвердить контракт слотов/каталога/брони для бота (#1016).
- Оплата e2e (hold→capture на completed), уведомления e2e.
- Analytics события пилота.

### BE — Bot (`ai-bot-platform`)
- **`link_ayla_service_ids`** команда + прогон + отчёт покрытия (снятие гейта #1034).
- Домёржить #1045 (hardening) → подготовить флип #1041 к включению по готовности каталога.
- AI orchestrator/persona ↔ ayla-ai-core wiring (booking + concierge).
- Food scanner: закрыть хвост (#956-994), consent/152-ФЗ (#956).
- Memory persistence (persona/context) на стороне бота.

### FE — Mini App (`ai-bot-platform/apps/miniapp`)
- Экраны: каталог услуг/мастеров, слоты, booking-flow, оплата, отзывы.
- Food: scan/result/insights + wellness/«День» (фикс #951).
- Profile/consent экраны (#946-953), surfacing персонализации (Память).
- AI-chat UI (если в miniapp, не только в боте).

### Shared — ayla-ai-core
- Orchestrator/tools/persona/prompts под booking + concierge.
- **context.py → UserPersonalContext**: контракт памяти (что пишем/читаем, зоны деликатности).
- Обновить README (устарел: пишет 0.1.0, по факту 0.8.1).

### Кросс-репо контракты
- Eventbus Ayla→bot (booking.*/payment.*) — уже выровнены (#946 в боте про ALLOWED_EVENT_NAMES — verify).
- ayla_service_id как ключ каталога (Ayla UUID) — единый.

## 5. Предложение по потокам/агентам (следующий шаг — «потом параллелим»)

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
- S1.6 `[A+B]` e2e-smoke miniapp→bot→Ayla: create/reschedule/cancel.

### S2 — AI Concierge 🟠
- S2.1 `[C]` orchestrator/persona/tools под booking+recommend (сверить актуальность 0.8.1).
- S2.2 `[B]` wire ayla-ai-core в бот DM (DRF-241) — диалог-подбор-бронь.
- S2.3 `[C]` inject personal-context в prompts (context_builder) ← связка с S3.
- S2.4 `[B]` recommendations (`/catalog/recommendations` уже зовётся фронтом) — verify.
- Deps: S1 (каталог), S3 (контекст).

### S3 — Memory / UserPersonalContext 🔴 (~60-70% готово)
- ✅ уже есть: модель + green-зона, GET/PATCH (DRF-174), **8 правил движка** (personalization_engine), behavioral Celery (infer_user_patterns), события.
- S3.1 `[A]` **wire 152-ФЗ endpoints** (POST /skip/, DELETE /<field>/, DELETE /) — код в personal_context_views есть, НЕ подключён.
- S3.2 `[A]` **yellow + red зоны**: поля + at-rest шифрование + red не в GET по умолчанию + retention 90д + отдельный access-log.
- S3.3 `[A]` включить Celery-beat для `infer_user_patterns` (source 2) — verify.
- S3.4 `[A/C]` source 3: structured-extraction WRITE из чата (сейчас только hint-read).
- S3.5 `[B/C]` concierge вызывает `should_ask_question` и **органично задаёт вопрос в DM** (source 1 end-to-end).
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
- S6.1 `[A]` оплата hold→capture на completed + webhook + refund — verify e2e.
- S6.2 `[A+B]` уведомления push/SMS + бот-DM на booking.*/payment.*.
- S6.3 `[A/B]` eventbus: booking.confirmed/no_show в ALLOWED_EVENT_NAMES (бот #946).

### S7 — Stabilization / Security / QA 🟡 (кросс, непрерывно)
- S7.1 ADR-0009 хвост (#928/#968/#1001).
- S7.2 e2e пилотные сценарии (booking / food daily / memory-ask).
- S7.3 security: token rotation Phase 0, red-zone guards (#1008).
- S7.4 pilot-readiness runbook (dual-system delete #937).

## 5.2 Волны параллелизации

- **Волна 1 (сразу, параллельно):** S1 (критпуть), S3 (независим, крупнейший), S4 (полиш), S5.2 (тесты).
- **Волна 2 (после S1):** S2 (нужен каталог+бронь), S6 (нужны booking-события), раз-stub miniapp booking.
- **S7 — непрерывно** через обе волны.
- Агенты: Backend Architect (S1-A/S3-A/S6-A) · general-purpose/Django (S1-B/S4-B) · AI Engineer (S2/S3.4-3.5) · Frontend Developer (S5/S4-M) · Security Engineer (S7).

## 6. Открытые вопросы
1. Дата пилота (ориентир был 2026-07-15 — актуально?).
2. Порог покрытия `ayla_service_id` для снятия гейта флипа (§#1034): для пилота Пензы реально ~100%.
3. Память: полный объём для этапа 1 или минимальный слой (пара полей: любимый мастер, адрес работы, бюджет)?
4. AI Avatar: финальное решение этап 1 vs 2 — по оценке сложности (S-стрим оценит).
