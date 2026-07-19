# Контрактная матрица пилота 2026-08-15

**Поток:** W6 · **Дата:** 2026-07-19 · **База контрактов:** PILOT_CONTRACTS_2026-08-15.md **v1.3.0** (C1–C5, R1, AMD-001…010).
**Доказательная база:** beautygo_backend `dev` @ `6defc583` + `pilot/billing` @ `268f2fcf` (W2, не смёржено); ai-bot-platform `dev` @ `6ff8d17` + `pilot/bot-backend` @ `2ec90ca` + `pilot/miniapp` @ `ab14adb` + `pilot/concierge` @ `f5a1fd0`; ayla-ai-core `main` @ `f773e7d` (v0.9.0).

**Легенда статусов:** ✅ done (реализовано + тесты) · 🟡 in-progress (код есть, не смёржено / не смонтировано) · ⛔ blocked (есть блокер) · ❌ absent (не начато).

## Сводка

| Контракт | Producer | Consumer | Статус end-to-end | Главный блокер |
|---|---|---|---|---|
| C1 Billing Eligibility (§2) | W2 🟡 | W1 ✅ | ⛔ | AMD-005 mismatch ключа + mount B-1 |
| C2 Billing Status (§3) | W2 🟡 | W3 ✅ / W4 ❌ | ⛔ | mount B-1/B-5; specialist mapping 503 |
| C3 Payout Preview (§4) | W1 ✅* | W3 ✅ / W4 ❌ | ⛔ | *ключ по SpecialistProfile.id, нужен резолвер AMD-005 |
| C4 Billing-события (§5) | W2 🟡 | W3 ✅ (log-only) | ⛔ | регистрация топиков R-2; emit-сайты activated/past_due ❌ |
| C5 Export/Delete 152-ФЗ (§6) | W2 🟡 / W3 ✅ | W4 🟡 / W6 ❌ (smoke) | 🟡 | merge W2; W4 API-wiring WIP |
| R1 Напоминания (§7) | W1 ✅ | W3 ✅ | ✅ (после flip внешней доставки) | OUTBOX_EXTERNAL_DELIVERY_TOPICS — решение оркестратора |

---

## C1. Billing Eligibility (§2, AMD-003, AMD-005)

| Сторона | Поток | Статус | Доказательства | Пробелы |
|---|---|---|---|---|
| Producer `can_accept_booking(specialist_id, tenant_id)` | W2 | 🟡 | `billing/services.py` (сигнатура по AMD-003 ✓, ключ = Ayla User UUID по AMD-005 ✓, fail-open ✓, salon-аккаунт блокирует всех мастеров ✓); тесты `billing/tests/test_services.py::TestCanAcceptBooking` (6) | приложение **не в INSTALLED_APPS**, urls не смонтированы — W1-патчи **B-1/B-5** |
| Consumer (гейт в create) | W1 | ✅ | `appointments/application/services/billing_eligibility.py` (importlib-импорт `billing.services`, fail-open + Sentry без ПДн), вызов в `create_booking_service.py:142`; 409 internal `SUBSCRIPTION_PAST_DUE` / клиент `UNAVAILABLE` нейтрально; блокируется только create; тесты `appointments/tests/test_billing_eligibility_c1.py` (7) | — |

**⛔ AMD-005 mismatch (критично):** адаптер W1 передаёт `SpecialistProfile.id`, W2 резолвит как **User UUID** → после merge проверка будет молча fail-open (записи не блокируются). Ни одна сторона тестами это не ловит. Требуется патч W1 (передавать `specialist.user_id`). Совместный инвариант-тест W1×W2 — обязателен (§2), волна 3.

## C2. Billing Status (§3, AMD-005)

| Сторона | Поток | Статус | Доказательства | Пробелы |
|---|---|---|---|---|
| Owner `GET /internal/billing/specialists/{id}/status/` | W2 | 🟡 | `billing/internal_api.py` + `internal_urls.py`; ключ User UUID ✓; 404 только `SPECIALIST_NOT_FOUND` ✓; 200 `status=none` с нулями ✓; тесты `billing/tests/test_internal_api.py` (4) | не смонтировано (B-1/B-5) |
| Consumer proxy в master_api | W3 | ✅ | `apps/master_api/services/billing.py`, `apps/integrations/ayla/billing_client.py` (путь точно по контракту, `data` verbatim); тесты `apps/master_api/tests/test_billing.py` (6) | fail-closed **503 `specialist_mapping_unavailable`** — закрывается резолвером AMD-005 на стороне W1 |
| UI (экран биллинга) | W4 | ❌ | — | экран, привязка карты (confirmation_url), инвойсы, consent автоплатежа — не начато |

## C3. Payout Preview (§4, AMD-004, AMD-005)

| Сторона | Поток | Статус | Доказательства | Пробелы |
|---|---|---|---|---|
| Owner `GET /internal/specialists/{id}/payout-preview/` | W1 | ✅* | `payments/views.py:1077` + `djangoProject/urls.py:49`; формула `sum(specialist_income)` по `scheduled`/`captured_pending_settlement` ✓; Decimal-строки 2зн HALF_UP ✓; hint «~следующий рабочий день…» ✓; пусто → 200 нули ✓; 404 только unknown ✓; тесты `payments/tests/test_payout_reconcile_c3.py` (5+5) | *резолв по `SpecialistProfile.id`; AMD-005 требует **User UUID** + внутренний резолвер — патч W1 |
| Consumer proxy в master_api | W3 | ✅ | как в C2 (`billing_client.py`, verbatim); тесты | тот же 503 mapping gap |
| UI «К выплате» | W4 | ❌ | — | карточка с разбивкой и формулировкой «ожидается» — не начато |

## C4. Billing-события в eventbus (§5, AMD-007, AMD-008, AMD-009)

| Сторона | Поток | Статус | Доказательства | Пробелы |
|---|---|---|---|---|
| Producer (outbox) | W2 | 🟡 | `billing/events.py`: 3 топика, полный envelope (AMD-007) с `event_id` UUID4 = PK (AMD-008) ✓; `BookingFee` OneToOne appointment = `UNIQUE(appointment_id)` ✓ (AYLA-DEC-0010); предикат онлайн-оплаты реализован | **топики не зарегистрированы** в `OutboxEvent.Topic`/`EVENT_VERSIONS` — W1-патч **R-2** (эмиссия сейчас ValueError → best-effort); emit-сайты `subscription.activated`/`past_due` ❌ (нет задач рекуррента/dunning — волна 2 W2); handler `on_booking_completed` ждёт регистрации **R-5** |
| Consumer (ingest + consumers) | W3 | ✅ | `POST /api/v1/internal/events/ingest`; envelope 10 полей; dedupe по `event_id` ✓; DLQ на unknown `event_version` ✓; `ALLOWED_EVENT_NAMES` включает все 3 billing-топика ✓; consumers `apps/eventbus/consumers/billing.py` с валидацией payload | consumers **log-only** — уведомления мастеру ждут specialist mapping gap |

**⚠️ AMD-009 отклонение:** контракт — предикат «нет Payment в `{authorized, paid}`»; W2 реализовал надмножество `{authorized, paid, refunded, partially_refunded}` (мотивировка: paid+полный refund не должен порождать fee — это уже учтено в AMD-009 текстом). Нужно решение оркестратора: принять и оформить amendment, или выровнять.

## C5. Personal Data Export/Delete (§6, AMD-006, AMD-010)

| Сторона | Поток | Статус | Доказательства | Пробелы |
|---|---|---|---|---|
| Ayla export/delete | W2 | 🟡 | `users/personal_data_api.py` на `pilot/billing`: `GET …/personal-data/export/` (sync JSON: закрытый subset профиля + полный personal-context, без lazy-create) ✓; `DELETE …/personal-data/` идемпотентный ✓ (AMD-006); аудит через `AnalyticsEvent personal_data_deleted` без значений ✓ (AMD-010); тесты `users/tests/test_personal_data_internal_api.py` (15) | не смёржено в dev (едет с веткой W2) |
| Bot агрегатор customer-facing | W3 | ✅ | `apps/identity/services/privacy.py`: export = Ayla + MemoryEntry + ConsentRecord (attachment) ✓; delete-каскад: upstream DELETE + memory soft-delete/tombstone + consent withdraw ✓; идемпотентно (upstream 404 = deleted) ✓; частичный сбой → честный 502 ✓; аудит без значений ✓; тесты `test_privacy.py` (9) | — |
| UI «Мои данные» | W4 | 🟡 | шторки в `CustomerProfileScreen.tsx` + support deeplink; test-first WIP: uncommitted `src/lib/personal-data.test.ts` на точные C5-endpoints | реализация `personal-data.ts` отсутствует — API не подключён |
| Dual-system smoke | W6 | ❌ | спецификация — `docs/qa/TEST_SPEC_W6_SMOKE.md` (волна 3) | runner `scripts/pilot_smoke/` — волна 3 |

## R1. Напоминания о записи (§7, AMD-007/008)

| Сторона | Поток | Статус | Доказательства | Пробелы |
|---|---|---|---|---|
| Producer booking-события | W1 | ✅ | outbox: `booking.created/confirmed/cancelled/rescheduled/completed/no_show` v1; envelope AMD-007/008 ✓; тесты emitter conformance (15+) | внешняя доставка **OFF**: `OUTBOX_EXTERNAL_DELIVERY_TOPICS` пуст по умолчанию (`settings/base.py:603`) — включение = решение+исполнение оркестратора (§5) |
| Доставка T−24h | W3 | ✅ | `apps/eventbus/consumers/booking.py`: upsert напоминаний на `booking.created`; идемпотентность `unique(ayla_appointment_id, kind)` ≡ `{appointment_id}:T-24h` ✓; cancel/reschedule обрабатываются идемпотентно ✓; тесты `test_booking_consumer.py` (4) | ⚠️ планируется **и T−2h** — за пределами пилотного сценария §7 (T−24h единственный). Решение оркестратора: оставить/отключить |

Замечание по таксономии: топик пишется `booking.cancelled` (double-l) — allowlist бота совпадает ✓ (зафиксировано, чтобы не «исправили» односторонне).

---

## Acceptance-чеклист §10 (готовность на 2026-07-19)

| # | Сценарий | Статус | Что закрывает / что осталось |
|---|---|---|---|
| 1 | Запись без предоплаты → напоминание T−24h → complete | 🟡 | W1: `payment_required=false` → CONFIRMED + `booking.confirmed` ✓ (тесты 6). W3: booking REST ✓, напоминания ✓. **GAP:** бот не передаёт `payment_required` (route-table проверяет только method/path/headers) — выбор «без предоплаты» из бота/miniapp невозможен контрактно (W3+W4) |
| 2 | Онлайн-оплата: hold → capture на complete → split 90₽/per-master (mock) | ✅ (backend) | W1: полный контур + 20 тестов (`test_capture_flow.py`, reconcile); e2e-smoke W6 — волна 3 |
| 3 | Мастер: карта → подписка → инвойс + «К выплате» (C2/C3) | ⛔ | W2: первый платёж (`save_payment_method`) и рекуррент ❌ (волна 2); C2/C3 backend 🟡/✅; W3 proxies ✅ (503 до mapping); W4 экраны ❌ |
| 4 | Офлайн complete → BookingFee 90₽ ровно один раз | 🟡 | W2: модель UNIQUE + `accrue_booking_fee` ✓ (тесты 7); активация handler'а ждёт R-5; совместный инвариант-тест W1×W2 — волна 3 |
| 5 | Отмена записи → холд отменён автоматически | ✅ | W1: `cancel_authorized_hold_for_appointment` (best-effort, отмена записи не блокируется) + тесты |
| 6 | 152-ФЗ export/delete из miniapp, dual-system | 🟡 | W2 ✅(не смёржено) / W3 ✅ / W4 🟡 WIP / W6 smoke ❌ (волна 3) |
| 7 | Память: вопрос → сохранено → рекомендация учла (consent-гейт) | ⛔ | ai-core v0.9.0 ✅; bot memory+consent ✅; **`should_ask` wiring в консьерже ❌** (W5; ветка `pilot/concierge` отстаёт от dev); бамп SHA в djangoproject ❌ |
| 8 | past_due → запись отклонена (C1), клиент видит нейтральное | ⛔ | обе стороны C1 ✅/🟡, но AMD-005 mismatch блокирует e2e; переход в past_due (dunning) ❌ (волна 2 W2) |

## Реестр блокеров/отклонений (вход для оркестратора)

| ID | Суть | Действие | Владелец |
|---|---|---|---|
| B-1/B-5 | billing app не в INSTALLED_APPS, urls не смонтированы | patch в settings/urls | W1 (по handoff W2) |
| R-2 | топики `subscription.activated/past_due`, `billing.fee_charged` не в `OutboxEvent.Topic`/`EVENT_VERSIONS` | регистрация | W1 |
| R-5 | `EVENT_HANDLERS`: `booking.completed` → `billing.handlers.on_booking_completed` | регистрация | W1 |
| K-1 | C1-адаптер передаёт `SpecialistProfile.id` вместо User UUID (AMD-005) | патч адаптера | W1 |
| K-2 | C3 payout-preview резолв по `SpecialistProfile.id` (AMD-005) | резолвер user→profile | W1 |
| D-1 | AMD-009: предикат реализован шире контракта (`+refunded, partially_refunded`) | принять+amendment или выровнять | оркестратор |
| G-1 | бот не передаёт `payment_required`; route-table не проверяет тела запросов | расширить route-table + клиент | W3 |
| D-2 | R1: бот планирует T−2h сверх контрактного T−24h | оставить/отключить | оркестратор |
| G-2 | specialist mapping (бот зеркалит по User UUID) → 503 на C2/C3-прокси, log-only C4-consumers | закрывается K-2 + проверка интеграции | W1→W3 |
| D-3 | включение `OUTBOX_EXTERNAL_DELIVERY_TOPICS` (booking.*, billing.*) | решение + исполнение | оркестратор (§5) |
