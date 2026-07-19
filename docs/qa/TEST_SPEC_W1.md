# Тест-спецификация W1 — Ayla Booking Core

**Поток:** W1 (beautygo_backend, `appointments/`, `payments/`) · **Версия:** 2026-07-19, W6.
**Основание:** PILOT_CONTRACTS v1.3.0 (C1, C3, R1, AMD-002/004/005/009), acceptance §10 сценарии 1, 2, 5.
**Правила:** реализацию пишет владелец потока; W6 верифицирует. Команда прогона: `pytest --ds=djangoProject.settings.test`.
**Маркировка:** ✅ тест существует (приёмочная верификация W6 пройдена) · ➕ тест должен быть добавлен · 🔶 зависит от решения оркестратора.

## W1-AC-01. Запись без предоплаты (§10.1, AMD-002)
- **Сценарий:** клиент создаёт запись с `payment_required=false`.
- **Шаги:** POST create (client и internal surface) → проверить статус и события → отменить запись.
- **Ожидаемое:** нет Payment; статус сразу CONFIRMED; события `booking.created` + `booking.confirmed`; отмена работает.
- **Тесты:** ✅ `test_views.py::TestAppointmentCreate::test_create_without_prepayment_confirms`, `test_create_default_keeps_online_payment_path`; ✅ `test_internal_booking_rest_1016.py::TestInternalCreateNoPrepayment` (3 теста).

## W1-AC-02. Онлайн-оплата: hold → capture → split 90₽ per-master (§10.2, D8/D9)
- **Сценарий:** запись с онлайн-оплатой проходит полный денежный контур (mock ЮKassa).
- **Шаги:** create (default) → AWAITING_PAYMENT + Payment → webhook authorized (hold) → `complete()` → отложенная capture-задача → capture выполнен → проверить transfers.
- **Ожидаемое:** `capture_at = min(completed_at + CAPTURE_DELAY_HOURS, expires_at − 60м)`, в пилоте delay=0; split: платформе ровно 90.00, мастеру `price − fee` на `SpecialistProfile.yookassa_account_id`; суммы — Decimal-строки 2 знака (§1); идемпотентный ключ capture стабилен (повтор — noop).
- **Тесты:** ✅ `test_capture_flow.py::TestFlatPlatformFee` (4), `TestCapturePlanning` (4), `TestCaptureOnComplete` (5), `TestWebhookCaptureState` (2).

## W1-AC-03. Flat-fee edge: услуга дешевле 90₽ (AMD-004)
- **Ожидаемое:** `platform_fee = min(90.00, price)`; `specialist_income = max(0, price − fee)`; отрицательные запрещены.
- **Тесты:** ✅ `TestFlatPlatformFee::test_fee_capped_at_amount`; ✅ `BookingSnapshot.create` cap (domain).

## W1-AC-04. Capture retry / reconciliation / алерты (D9)
- **Шаги:** transient-ошибка capture → retry с backoff (60с→16мин, jitter, ×5) → exhausted → `capture_failed`; reconciliation job лечит «застрявшие» completed; алерт при приближении `expires_at`; ручной повтор `python manage.py retry_capture [--payment-id] [--sync]`.
- **Тесты:** ✅ `TestCaptureOnComplete` (transient/exhausted), `test_payout_reconcile_c3.py::TestReconcileCaptures` (5), `TestRetryCaptureCommand` (2).

## W1-AC-05. Отмена записи → холд отменён (§10.5)
- **Ожидаемое:** при cancel вызывается ЮKassa `/cancel` для authorized-холда, best-effort: сбой провайдера НЕ блокирует отмену записи; идемпотентный ключ `cancel-{payment.id}`.
- **Тесты:** ✅ `TestCancelReleasesHold` (2).

## W1-AC-06. Нет суб-счёта мастера → 422, но без предоплаты можно (D8)
- **Ожидаемое:** онлайн-оплата без `yookassa_account_id` → 422 `ONLINE_PAYMENT_UNAVAILABLE`; `payment_required=false` — работает.
- **Тесты:** ✅ `TestOnlinePaymentUnavailable` (2).

## W1-AC-07. C1-гейт в create (§2, §10.8)
- **Ожидаемое:** `past_due` → internal 409 `SUBSCRIPTION_PAST_DUE`, клиент 409 `UNAVAILABLE` + нейтральный текст (без долга); отсутствие модуля billing / техсбой → fail-open + Sentry без ПДн; блокируется только create (cancel/reschedule/complete — нет); idempotency-replay не блокируется.
- **Тесты:** ✅ `test_billing_eligibility_c1.py` (7).
- **➕ Пробел K-1:** адаптер передаёт `SpecialistProfile.id`, W2 ожидает **User UUID** (AMD-005). После патча нужен тест `test_adapter_passes_user_uuid` (assert аргумента вызова = `specialist.user_id`).

## W1-AC-08. C3 Payout Preview (§4)
- **Ожидаемое:** `pending_amount = sum(specialist_income)` по `scheduled`+`captured_pending_settlement`; пусто → 200 `"0.00"` + hint null; 404 только unknown specialist; Bearer-auth; Decimal-строки; UX-формулировки «ожидается».
- **Тесты:** ✅ `test_payout_reconcile_c3.py::TestPayoutPreview*` (5) + auth (2).
- **➕ Пробел K-2:** резолв по `SpecialistProfile.id`; после патча AMD-005 — тест `test_resolves_specialist_by_user_uuid` (запрос по User UUID → тот же агрегат).

## W1-AC-09. R1 producer: booking-события (§7, AMD-007/008)
- **Ожидаемое:** create/confirm/cancel/reschedule/complete/no_show эмитят `booking.*` v1 с полным envelope; `event_id` UUID4 стабилен (retry — тот же event); внешняя доставка только для топиков из `OUTBOX_EXTERNAL_DELIVERY_TOPICS` (default пусто 🔶 D-3).
- **Тесты:** ✅ `test_emitter_conformance_196.py` (6), `test_external_delivery_gate.py` (5), outbox publisher/HMAC/e2e/replay (4 файла).

## W1-AC-10. Совместный инвариант W1×W2: fee ровно один раз (§10.4, AYLA-DEC-0010, AMD-009) — волна 3
- **Сценарий:** (а) online-paid completed → BookingFee НЕ создаётся (90₽ удержан split'ом); (б) offline completed → BookingFee создан ровно один раз; (в) Payment в `failed/pending` (брошенные) — не считается онлайн-оплатой.
- **Тесты:** ✅ W1-сторона `TestSingleFeeInvariantW1Side::test_online_paid_booking_has_flat_90_fee`; ➕ **совместный инвариант-тест** (обязателен по §2/§10) — кандидат в `scripts/pilot_smoke/` (W6) + юнит на стороне W2 после R-5. 🔶 D-1: предикат W2 шире контракта (`+refunded/partially_refunded`).
