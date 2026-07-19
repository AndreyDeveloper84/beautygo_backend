# Тест-спецификация W4 — Mini App FE

**Поток:** W4 (ai-bot-platform `apps/miniapp`, TypeScript/React) · **Версия:** 2026-07-19, W6.
**Основание:** PILOT_CONTRACTS v1.3.0 (C5.1/6.2 UI, §4 UI-формулировки, AMD-002 UI выбора оплаты), acceptance §10 сценарии 1, 3, 6; бриф W4 (152-ФЗ, un-stub #856, billing-экран, «К выплате», consent).
**Правила:** реализацию пишет W4; W6 верифицирует. Команды: `npm ci && npm run typecheck`; `npm test -- --run` (vitest).
**Маркировка:** ✅ существует (ветка `pilot/miniapp`) · ➕ должен быть добавлен · 🔶 зависит от решения/другого потока.

## W4-AC-01. 152-ФЗ export UI (§10.6)
- **Сценарий:** «Мои данные» → «Скачать данные» → файл JSON у пользователя.
- **Шаги:** открыть профиль → запросить export → дождаться 200 → скачивание (`triggerDownload`) → ошибка сети → понятный retry.
- **Ожидаемое:** вызов `GET /api/v1/customer/me/personal-data/export/`; скачивается валидный JSON с 3 секциями (ayla/bot/consents); состояние загрузки и ошибки.
- **Тесты:** 🟡 WIP `src/lib/personal-data.test.ts` (uncommitted, test-first: `exportPersonalData`, `triggerDownload`) — ➕ реализация `personal-data.ts` + компонентный тест шторки (сейчас кнопки ведут в support-sheet — заменить на прямой API).

## W4-AC-02. 152-ФЗ delete UI (§10.6)
- **Шаги:** «Удалить аккаунт» → подтверждение (явное, не одним тапом) → DELETE → статус «удалено» → повторный вход — данных нет.
- **Ожидаемое:** вызов `DELETE /api/v1/customer/me/personal-data/`; обработка 502-partial (`PersonalDataPartialDeleteError`) с честным статусом «удалено частично, повторите/поддержка»; поддержка-deeplink `https://max.me/aylasupport` доступен из ошибки.
- **Тесты:** 🟡 WIP тем же файлом; ➕ компонентный тест подтверждения и partial-состояния.

## W4-AC-03. Booking flow на реальном API (#856, §10.1)
- **Ожидаемое:** слоты/создание/перенос/отмена через bot API (без stub); cancel — request/confirm/undo; reschedule — request/confirm; пустые/ошибочные состояния.
- **Тесты:** ✅ `state/booking.test.ts`, `lib/booking-status.test.ts` (+ `api.ts` реальные вызовы); ➕ e2e staging (волна 3, smoke W6: booking CRUD miniapp→bot→Ayla).
- **Остаток stub:** `customer-booking.ts` (рекомендации, ждёт endpoint), `customer-records.ts` — 🔶 зафиксировано, не блокер пилота.

## W4-AC-04. Выбор онлайн-оплаты при создании (AMD-002, D6) — после G-1
- **Ожидаемое:** при create пользователь может выбрать «оплатить онлайн» или «на месте»; выбор маппится в `payment_required` true/false; без платёжного экрана при `false` — сразу CONFIRMED-экран.
- **Тесты:** ➕ `booking-payment-choice.test.ts`: обе ветки маппинга, default = онлайн (true). 🔶 зависит от G-1 (W3 передаёт поле).

## W4-AC-05. Экран биллинга мастера (§10.3, C2)
- **Ожидаемое:** статус подписки (trial/active/past_due/canceled/none), `current_period_end`, `next_charge` (сумма/дата), инвойсы; привязка карты — переход по `confirmation_url` (вне MAX, зафиксированная фрикция) и возврат; past_due → экран погашения (без лишних деталей клиентам — это кабинет мастера).
- **Тесты:** ➕ `master-billing.test.tsx`: рендер всех 5 статусов, формат сумм (2 знака, ₽), состояние загрузки/ошибки 503 (`specialist_mapping_unavailable` → «скоро доступно», не crash).

## W4-AC-06. Карточка «К выплате» (§4 UI, §10.3)
- **Ожидаемое:** сумма `pending_amount` + разбивка по записям; состояния различены явно: `scheduled` → «Ожидает подтверждения после визита», `captured_pending_settlement` → «Подтверждено, ожидает перечисления»; дата — «ожидается ~… (по расписанию ЮKassa)».
- **Тесты:** ➕ `payout-card.test.tsx`: оба состояния, пустое состояние (0.00), и **copy-тест: в строках отсутствует слово «гарантированно»** (контракт §4: формулировки «ожидается»).

## W4-AC-07. Consent на автоплатёж (D7, бриф W4.4) — после W2/legal
- **Ожидаемое:** в флоу привязки карты — чекбокс с текстом оферты (источник текста: W2/legal); submit заблокирован без отметки; факт consent уходит в API (document_version).
- **Тесты:** ➕ `autopay-consent.test.tsx`: блокировка submit, наличие текста оферты, передача версии документа.

## W4-AC-08. Базовое качество FE (DoD)
- **Ожидаемое:** `npm run typecheck` чисто; критичные флоу покрыты vitest; тесты в CI.
- **Тесты:** ✅ vitest сконфигурирован, 4 committed файла тестов; ➕ довести набор до критичных флоу (booking, consent, billing, personal-data) и подключить `npm test -- --run` в CI (контракт §12).
