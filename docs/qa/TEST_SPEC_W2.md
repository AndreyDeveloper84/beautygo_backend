# Тест-спецификация W2 — Ayla Billing & Legal

**Поток:** W2 (beautygo_backend, новое приложение `billing/` + `users/personal_data_api.py`) · **Версия:** 2026-07-19, W6.
**Основание:** PILOT_CONTRACTS v1.3.0 (C1, C2, C4, C5, AMD-003/004/005/006/009/010; AYLA-DEC-0001/0007/0010), acceptance §10 сценарии 3, 4, 6, 8.
**Правила:** реализацию пишет W2; W6 верифицирует. Команда прогона: `pytest --ds=djangoProject.settings.test`.
**Маркировка:** ✅ существует (на ветке `pilot/billing` @ `268f2fcf`) · ➕ должен быть добавлен · 🔶 зависит от решения оркестратора.

## W2-AC-01. Тарифы и модели (D1)
- **Ожидаемое:** seed `TariffPlan` solo 690.00 / salon 990.00; `BookingFee` — `UNIQUE(appointment_id)`; `BillingInvoice`/`BillingPayment` — уникальный `idempotency_key`; `BillingConsent` — один активный на пользователя.
- **Тесты:** ✅ `test_models.py` (TestTariffPlanSeed, TestBookingFeeInvariant, TestBillingInvoice, TestBillingPayment, TestBillingConsent — 11).

## W2-AC-02. C1 `can_accept_booking` (§2, AMD-003/005)
- **Ожидаемое:** нет аккаунта/техсбой → `ok=True` (fail-open); `past_due` → `ok=False, reason=SUBSCRIPTION_PAST_DUE`; salon-аккаунт блокирует всех мастеров tenant'а; tenant без salon-подписки → личная подписка; ключ = User UUID.
- **Тесты:** ✅ `test_services.py::TestCanAcceptBooking` (6), `TestResolveBillingAccount` (3).

## W2-AC-03. BookingFee на complete, предикат онлайн-оплаты (§10.4, AMD-009)
- **Ожидаемое:** fee начисляется на `booking.completed` только если по записи нет Payment в `{authorized, paid}`; ровно один раз (get_or_create + UNIQUE); edge `min(90, price)`; нет аккаунта → Sentry reconciliation-инцидент, не raise.
- **Тесты:** ✅ `TestHasOnlinePayment` (3), `TestAccrueBookingFee` (7), `test_handlers.py::TestOnBookingCompleted` (4).
- **Блокеры:** 🔶 D-1 (предикат шире: `+refunded, partially_refunded` — amendment или выровнять); активация handler'а ждёт R-5 (W1); после активации ➕ интеграционный тест «real outbox → fee начислен».

## W2-AC-04. C2 Billing Status endpoint (§3)
- **Ожидаемое:** 404 только unknown specialist; 200 `status=none` + нули без аккаунта; 200 active: все поля §3, суммы Decimal-строки 2 знака, даты ISO; ключ User UUID; Bearer.
- **Тесты:** ✅ `test_internal_api.py` (4). ➕ после mount (B-1/B-5): smoke на реальном `djangoProject/urls.py` (не shim `urls_w2.py`).

## W2-AC-05. Первый платёж мастера (§10.3, D7) — волна 2
- **Сценарий:** мастер привязывает карту → автосписание подписки.
- **Шаги:** create setup-платёж `save_payment_method: true` → confirmation_url → (mock) оплата → webhook `payment.succeeded` → `payment_method_id` сохранён.
- **Ожидаемое:** идемпотентность (прецедент `X-Idempotency-Key` fallback); повторный webhook — noop; consent автоплатежа зафиксирован (BillingConsent, document_version).
- **Тесты:** ➕ `test_first_payment.py`: happy path, идемпотентный повтор webhook, отказ без consent, чек 54-ФЗ (плательщик=мастер, receipt-builder).

## W2-AC-06. Рекуррентное списание (monthly beat) (§10.3, D7) — волна 2
- **Ожидаемое:** сумма = подписка + Σ BookingFee за период; идемпотентный ключ на период+аккаунт; успех → `subscription.activated` (продление) + `billing.fee_charged` события в outbox (конверт AMD-007/008, стабильный `event_id`, retry без нового event).
- **Тесты:** ➕ `test_recurrent_charge.py`: расчёт суммы, идемпотентность повторного запуска beat, эмиссия событий (после R-2 — на реальном outbox), ноль fee → только подписка.

## W2-AC-07. Dunning → past_due → блокировка записей (§10.8, D7) — волна 2
- **Шаги:** fail списания → retry T+1d → fail → retry T+3d → fail → статус `past_due` + событие `subscription.past_due` → C1 блокирует новые записи (существующие/перенос/отмена — нет).
- **Тесты:** ➕ `test_dunning.py`: переходы по осям времени (freezegun), ровно 2 retry, эмиссия `past_due` один раз, разблокировка после оплаты; e2e с C1 — волна 3 (совместный W1×W2).

## W2-AC-08. C4 producer-контракт (§5, AMD-007/008)
- **Ожидаемое:** топики `subscription.activated`, `subscription.past_due`, `billing.fee_charged`; payload по §5; конверт полный; неизвестные доп. поля допустимы (forward compat), обязательные не отсутствуют.
- **Тесты:** ➕ после R-2 (регистрация топиков): `test_events_outbox.py` — эмиссия каждого топика пишет OutboxEvent с конвертом по AMD-007/008; `event_id` = PK UUID4; повторная эмиссия не дублирует бизнес-событие (UNIQUE appointment_id для fee_charged).

## W2-AC-09. C5 export (§6.1, AMD-006)
- **Ожидаемое:** `GET /api/v1/internal/users/{id}/personal-data/export/` → 200 sync JSON: закрытый subset профиля + полный каталог personal-context; нет контекста → `null`, без lazy-create; Bearer.
- **Тесты:** ✅ `test_personal_data_internal_api.py::TestExport` (3) + auth (5).

## W2-AC-10. C5 delete + аудит (§6.2, AMD-006/010)
- **Ожидаемое:** `DELETE …/personal-data/` — каскад wipe personal-context; повтор → 200 (идемпотентно); soft-deleted user → 404; аудит через `AnalyticsEvent personal_data_deleted` (actor, timestamp, scope) **без значений**, пишется и на повторе; транзакционные записи (записи/платежи) не трогаем (вне scope пилота).
- **Тесты:** ✅ `TestDelete` (5). ➕ регрессия: delete не затрагивает appointments/payments ряды (assert count unchanged).

## W2-AC-11. Приватность долга (§2)
- **Ожидаемое:** код `SUBSCRIPTION_PAST_DUE` уходит только internal; клиентский API — generic `UNAVAILABLE`; Sentry-записи без персональных/платёжных данных.
- **Тесты:** ✅ W1-side `test_client_create_generic_unavailable_no_debt_disclosure`; ➕ W2-side: assert логов/Sentry-пейлоада на отсутствие ПДн при fail-open.
