# PILOT PROGRESS — готовность к пилоту 2026-08-15

Обновлено: 2026-07-19 (день 2 из 28). Владелец: оркестратор. Обновляется после каждого отчёта потоков.

**Правила подсчёта:**
- Оценки в Story Points (1/2/3/5/8) — сложность, не часы.
- ✅ done = код в dev (смержено и запушено). 🔄 in-progress = в работе. ⏳ pending = не начато/заблокировано.
- Готовность = done SP / total SP. In-progress показан отдельно — в проценты НЕ включается (честно).
- Внешние задачи (юрист, KYC, набор мастеров) считаются отдельно от кода.

## Общий прогресс

```
Код:     ████████████░░░░░░░░░░░░░░  44% done (80/182 SP)
         ░░░░████░░░░░░░░░░░░░░░░░░  16% in-progress (29 SP)
         ░░░░░░░░░░████████████░░░░  40% pending (73 SP)
```

- **Done:** 80 SP · **In-progress:** 29 SP · **Pending (код):** 57 SP · **Pending (внешнее):** 16 SP
- Осталось дней: **26** (пилот 15.08). Сделано за дни 1–2: 78 SP.
- Требуемый темп: ~4 SP/день суммарно по окнам — достижимо при текущей скорости.

## По потокам

| Поток | Готовность | Done SP | В работе | Осталось | Комментарий |
|---|---|---|---|---|---|
| W1 Booking Core | **84%** | 27/32 | — | 5 SP (follow-up патчи — ждут W2) | ✅ в dev; очередь P1–P7 готова |
| W2 Billing | **0%** | 0/30 | 11 SP | 19 SP | 🔥 главная текущая работа |
| W3 Bot Backend | **88%** | 29/33 | — | 4 SP (follow-up) | ✅ в dev + #1045 |
| W4 Mini App | **32%** | 10/31 | — | 21 SP | ✅ 152-ФЗ + stub-gate в dev; home-решение принято |
| W5 Concierge | **15%** | 3/20 | 17 SP | — | 🔥 фаза 2 запущена (память) |
| W6 QA/Docs | **55%** | 11/20 | 1 SP | 8 SP (волна 3) | ✅ документы в Git |
| **ИТОГО код** | **44%** | **80/182** | 29 SP | 57 SP | |
| Внешнее | **0%** | 0/16 | — | 16 SP | юрист/KYC/мастера |

## W1 — Ayla Booking Core (84%)

| Задача | SP | Статус |
|---|---|---|
| Sync + merge memory-ветки | 2 | ✅ |
| Запись без предоплаты (D6) | 3 | ✅ |
| Каталог: поля template_id/name/category_slug (C6) | 3 | ✅ |
| Capture pipeline (D9) | 5 | ✅ |
| Авто-отмена холда | 2 | ✅ |
| Flat 90₽ | 1 | ✅ |
| Split per-master | 3 | ✅ |
| Reconciliation + алерты | 3 | ✅ |
| Payout preview (C3) | 2 | ✅ |
| Eligibility adapter (C1) | 2 | ✅ |
| Бамп ai-core v0.9.0 | 1 | ✅ |
| Follow-up патчи P1–P7 (INSTALLED_APPS, beat, urls, топики, handler, совместный тест, слоты-offset) | 5 | ⏳ после merge W2 |

## W2 — Billing & Legal (0%)

| Задача | SP | Статус |
|---|---|---|
| Модели (TariffPlan, Subscription, BookingFee, Invoice, Payment, Consent) | 5 | 🔄 |
| C5 export/delete endpoints | 3 | 🔄 |
| C1 can_accept_booking | 3 | 🔄 |
| Первый платёж + save_payment_method | 3 | ⏳ |
| Рекуррент monthly charge | 5 | ⏳ |
| Dunning → past_due | 3 | ⏳ |
| Чеки 54-ФЗ платформа→мастер | 2 | ⏳ |
| C2 status endpoint | 2 | ⏳ |
| C4 события | 2 | ⏳ |
| Совместный инвариант-тест W1×W2 | 2 | ⏳ |

## W3 — Bot Backend (88%)

| Задача | SP | Статус |
|---|---|---|
| Import cycle fix + baseline | 1 | ✅ |
| link_ayla_service_ids + тесты | 5 | ✅ (прогон — staging) |
| Route-table idempotency pins | 1 | ✅ |
| C1 нейтральный surface | 1 | ✅ |
| Merge memory-consent-global | 2 | ✅ |
| Inferred memory persistence | 3 | ✅ |
| Personal-context client | 3 | ✅ |
| C4 топики + consumers | 2 | ✅ |
| R1 напоминания (верификация) | 1 | ✅ |
| C5 privacy endpoints | 3 | ✅ |
| C2/C3 прокси master_api | 2 | ✅ |
| Baseline-rot fixes | 2 | ✅ |
| #1045 разрешение конфликта | 2 | ✅ |
| Бамп ai-core v0.9.0 | 1 | ✅ |
| payment_required на create (G-1) | 1 | ⏳ |
| link до AMD-001 (tiebreaker, stripping, mapping file) | 2 | ⏳ |
| Прогон покрытия на staging | 1 | ⏳ staging |

## W4 — Mini App (26%)

| Задача | SP | Статус |
|---|---|---|
| Vitest + первые тесты | 3 | ✅ |
| C5 шторки export/delete | 3 | ✅ |
| Аудит 48 экранов | 2 | ✅ |
| Коммит 3: скрыть stub-секции | 2 | ✅ |
| Экран биллинга мастера (2б) | 5 | ⏳ W2+резолвер |
| Карточка «К выплате» (2б) | 2 | ⏳ W2+резолвер |
| Booking flow на реальном API (3) | 5 | ⏳ |
| UX-статусы оплаты (3) | 2 | ⏳ |
| C1 нейтральное сообщение (3) | 1 | ⏳ |
| Profile polish + notification prefs | 3 | ⏳ |
| Stub-экраны → реальные данные | 3 | ⏳ |

## W5 — Concierge (15%)

| Задача | SP | Статус |
|---|---|---|
| Фаза 1: релиз ai-core v0.9.0 | 3 | ✅ |
| Concierge wiring (DRF-241) | 5 | 🔄 |
| Memory block injection + consent-гейт | 3 | 🔄 |
| Memory-ask (S3.5) | 5 | 🔄 |
| Голос/границы (Конституция) | 2 | 🔄 |
| Orchestrator baseline-rot (9 тестов) | 2 | 🔄 |

## W6 — QA/Docs (55%)

| Задача | SP | Статус |
|---|---|---|
| Документы в Git | 1 | ✅ |
| Baseline + контрактная матрица | 2 | ✅ |
| Тест-спецификации W1–W5 | 3 | ✅ |
| Черновики (Killer PRD, Memory Lifecycle) | 3 | ✅ |
| Roadmap rebase | 2 | ✅ |
| Smoke-runner | 5 | ⏳ волна 3 |
| Runbook | 3 | ⏳ волна 3 |
| Drift-контроль (еженедельно) | 1/нед | 🔄 |

## Внешние задачи (0%)

| Задача | SP-экв | Владелец | Дедлайн |
|---|---|---|---|
| Оферта автоплатежа | 2 | юрист | 01.08 |
| Агентская формулировка чеков | 1 | юрист | 01.08 |
| Правки 3/5 cross-domain правил | 2 | юрист | 01.08 |
| KYC-онбординг мастеров в ЮKassa | 3 | ops+юрист | 08.08 |
| Набор 15+ мастеров (supply) | 5 | основатель | 08.08 |
| Staging: прогон link + флип гейта | 3 | оркестратор | нед. 3 |

## Журнал обновлений

- **2026-07-19 (день 2, вечер):** +2 SP — W4 коммит 3 (stub-gate prod) в dev `7b56816`. W4 → 32% (10/31). Решение по home-маршруту: interim ComingSoonCard → цель «Мои записи»; catalog fake-data → gate до фазы 3. Done: 80/182 (44%).
- **2026-07-19 (день 2):** стартовый снимок. Done 78 SP (43%): W1 ✅ (27), W3 ✅ (29), W4 частично (8), W5 фаза 1 (3), W6 (11). Парный бамп ai-core v0.9.0 замкнут. Контракты v1.4.0.
