# Спринт 2 — Staging Integration (v2, переработан по review 2026-07-20)

Статус: v2 после review «NEEDS REVISION» — применены все 18 обязательных правок.
Владелец: оркестратор. Трекер: `docs/qa/PILOT_PROGRESS.md`.
Связанные документы: `docs/SPRINT_2_STAGING_SETUP.md` (v2), `docs/PILOT_CONTRACTS_2026-08-15.md` (v1.9.0), `docs/qa/PILOT_RUNBOOK.md`, `docs/EXTERNAL_QUESTIONS.md`.

## Цель спринта

Сквозной путь пилота работает на staging end-to-end:
сообщение боту → подбор мастера → запись через Ayla REST (флип #1041 ON) →
онлайн-оплата тестовой картой (hold → capture → split) → напоминание T−24h →
memory-ask → события Ayla→бот + REST вызовы бот→Ayla → корректные деньги
(platform_fee 90₽ платформе; доход мастера = цена − 90₽).

## Реестр контрактов (traceability)

Все ссылки в этом документе — на FROZEN контракты из
`docs/PILOT_CONTRACTS_2026-08-15.md` (v1.9.0, в dev):

| ID | Название | Документ | Статус |
|---|---|---|---|
| C1 | Billing Eligibility (can_accept_booking) | PILOT_CONTRACTS §2 | FROZEN |
| C2 | Billing Status | §3 | FROZEN |
| C3 | Payout Preview | §4 | FROZEN |
| C4 | Billing-события eventbus | §5 | FROZEN |
| C5 | Personal Data Export/Delete (152-ФЗ) | §6 | FROZEN |
| C7 | Client Payments (hold/cards/statuses) | §7.5 | REVIEW (границы закрыты в тексте v1.7.0; код W1/W3/W4 реализован) |
| R1 | Напоминания T−24h (+T−2h) | §7 | FROZEN |
| AMD-001…016 | Amendments (по одному на решение) | §14 | APPROVED |
| AYLA-DEC-0011 | Опциональный штатный сценарий онлайн-оплаты | ayla-knowledge/02 Strategy | действует |

Eventbus — только направление **Ayla→бот** (outbox). Обратное направление в
пилоте — **REST internal API** (booking create, personal-context, payments),
не event-канал. Секрет HMAC — один на одно направление (Ayla outbound ↔ bot
ingest). Топики (D-3): booking.created/confirmed/cancelled/rescheduled/completed,
payment.captured/failed/refunded, subscription.activated/past_due,
billing.fee_charged.

## Gate 0 — внешняя готовность (до кодового дня)

| # | Критерий | Владелец |
|---|---|---|
| E1 | Выбран VPS и доменная схема (§setup) | Founder |
| E2 | Тестовый магазин ЮKassa доступен (чеклист §2 setup) | Founder |
| E3 | MAX staging-бот создан (§3 setup) | Founder |
| E4 | Список мастеров утверждён (≥15, KYC-трек ниже) | Founder |
| E5 | Staging security baseline утверждён (§4 setup) | оркестратор |

Если нет E2 или E3 — спринт запускается частично, DoD-дата = AT RISK.

## Gate 1 — инфраструктура

- docker-compose.staging.yml (без конфликтов портов, см. setup §1.1);
  `docker compose config` проверен до запуска; одна модель управления —
  **compose под systemd** (systemd управляет проектом, не дублирует процессы).
- DNS/TLS: api-staging / bot-staging / app-staging (или routing-таблица по
  существующим доменам — setup §6).
- Firewall: наружу только 80/443 (+22 с allowlist).
- Secrets: отдельные staging, 0600, вне Git, не копии production.
- Миграции (порядок: appointments 0012/0013, payments 0003/0004, users 0014,
  billing 0001/0002) + backup/снапшот БД ДО деплоя + проверка восстановления.
- Mini App deploy (S0.4): `npm ci` → `npm run build` → dist статика (nginx,
  cache headers, без source maps в проде, CSP, VITE_* сборки, проверка
  MAX launch context).
- **Версии зафиксированы** (никакого «деплой ветки dev»):

| Компонент | Репозиторий | SHA/image |
|---|---|---|
| Ayla | beautygo_backend | `<sha>` |
| Bot | ai-bot-platform | `<sha>` |
| AI Core | ayla-ai-core | v0.9.0 / `f773e7d` |
| Mini App | ai-bot-platform | `<sha>` |
| DB schema | migration heads | `<list>` |

## Gate 2 — техническая интеграция

- **Покрытие каталога (знаменатель зафиксирован):** 100% **активных и доступных
  для бронирования услуг пилотных мастеров** имеют Ayla service_id/template_id.
  Отчёт обязан содержать: `eligible_for_pilot`, `linked`, `unlinked`,
  `excluded_with_reason`, `coverage_percent`. **Флип — только при `unlinked = 0`.**
- Флип `BOOKING_VIA_AYLA_REST` на staging (после coverage).
- События доставляются (Ayla→бот), idempotency по event_id, DLQ-дрель
  (event_version: 2 → DLQ + alert, очередь жива, тестовая запись помечена).
- Память: consent → ask → PATCH → факт в блоке промпта; без memory_green —
  блока нет.
- Mini App вызывает реальные endpoints (никаких stub-данных в проде).

## Gate 3 — деньги (тестовый магазин)

- hold → complete → capture → **split: platform_fee 90₽ платформе,
  доход мастера = цена − 90₽** (transfers[].account_id + platform_fee_amount).
- Отмена записи → hold canceled.
- **Offline (без предоплаты):** complete → BookingFee 90₽ **ровно один раз**
  (инвариант: никогда split + BookingFee на одной записи — AYLA-DEC-0010).
- Подписка: card-setup → charge → invoice; accelerated dunning (механизм
  ускорения времени, §тестинг) → past_due → C1 блокирует новую запись;
  pay-debt → active.
- Webhook verification: IP-allowlist + Basic; повтор webhook идемпотентен;
  реконсиляция через GET состояния платежа.
- `YOOKASSA_AGENT_ID` — **верифицировать против реального кода и договорной
  схемы** (текущий transfers на общий sub-account vs per-master account_id из
  SpecialistProfile) — отдельная задача проверки до S3.

## Gate 4 — acceptance

Smoke-матрица (заменяет «7 сценариев»):

| ID | Сценарий | Тип |
|---|---|---|
| SM-01 | Booking без предоплаты (create→CONFIRMED) | auto |
| SM-02 | Hold → complete → capture → split | auto + test YK |
| SM-03 | Cancel → hold canceled | auto |
| SM-04 | Offline complete → BookingFee ровно один раз | auto |
| SM-05 | Subscription → invoice → past_due → C1 → pay-debt | accelerated |
| SM-06 | Memory consent → ask → PATCH → рекомендация учла | auto |
| SM-07 | Event delivery → duplicate → DLQ | auto |
| SM-08 | Dual export/delete (Ayla+bot+memory, идемпотентно) | auto |
| SM-09 | T−24h reminder без дублей | accelerated |
| UX-01 | Клиентский booking flow в miniapp | manual |
| UX-02 | Кабинет мастера (billing, payout, карта) | manual |
| OPS-01 | Rollback rehearsal (процедура ниже) | manual/runbook |

**Ускорение времени** (для T−24h, T+1d/T+3d): clock abstraction /
staging-only настройки интервалов / management-команды / фабрики дат.
**Запрещено** менять системное время VPS. После drill — возврат к
production-like значениям, проверка конфигурационным тестом.

**Негативные сценарии (обязательные):** повтор create с одним idempotency key;
slot занят; услуга без link; истёкший internal token; неверный HMAC; webhook
повторён; webhook устаревшего статуса; неизвестный payment id; повтор после
capture; event receiver 500; ЮKassa timeout после capture; мастер без
account_id; долг мастера не раскрыт клиенту; без memory consent; delete повторно.

**Observability до smoke:** структурированные логи с correlation_id; Sentry
environment staging + release SHA; метрики: очереди Celery, outbox backlog,
DLQ count, capture failures, webhook failures, payment nearing expires_at,
worker heartbeat, disk/RAM, DB connections, TLS expiration. Алерты:
readiness failed, worker/beat unavailable, outbox oldest age > threshold,
DLQ > 0, capture_failed > 0, payment → expires_at, disk > 80%, OOM, 5xx rate.

## Gate 5 — pilot readiness

- **Мастера: gate = 10 полностью готовых (KYC active + услуги + расписание +
  тест-запись); stretch = 15.** Цель к пилоту 15.08 — отдельно.
- Юридические тексты (оферта, агентские чеки, cross-domain правки).
- Support-контакты (deeplink handle — ops, env VITE_SUPPORT_DEEPLINK).
- Go/No-Go review по evidence (SHA, smoke-отчёты, метрики).

## Rollback — исполняемая процедура (OPS-01)

1. Выключить `BOOKING_VIA_AYLA_REST` (флаг назад — без даунтайма).
2. Остановить beat (нет новых задач).
3. Зафиксировать очередь и незавершённые платежи (список на reconciliation).
4. Откатить приложение на предыдущий зафиксированный image/SHA.
5. Миграции НЕ откатывать автоматически (аддитивные — безопасны с кодом назад;
   seed billing/0002 обратим вручную).
6. Health/readiness проверка.
7. Создать запись по fallback-path (локальный путь бота) — проверить.
8. Возобновить worker/beat.
9. Reconciliation: холды (retry_capture), outbox, DLQ, charge-состояния.
Критерий успеха: запись создаётся, платежи в известном состоянии, дублей нет.
Обязательно: snapshot БД перед деплоем + репетиция восстановления.

## KYC — отдельный operational track (не в SP-бэклоге)

| Шаг | Содержание | Статусы |
|---|---|---|
| KYC-01 | Список мастеров (E4) | not_started → approved |
| KYC-02 | Данные собраны (ИНН, реквизиты, статус самозанятый/ИП) | submitted |
| KYC-03 | Заявки в ЮKassa (суб-аккаунты) | verification_pending |
| KYC-04 | account_id активны и в `SpecialistProfile` | active / rejected / blocked |
| KYC-05 | Услуги + расписание наполнены | done |
| KYC-06 | Тест-запись на каждого (визит → деньги верны) | done |

В пилот допускаются только `active`. Кодовый спринт не считается проваленным
из-за ожидания KYC; pilot readiness (Gate 5) — считается.

## Реалистичность (без SP-скорости)

Кодовая ёмкость достаточна по предварительной оценке. Срок определяется
критическим путём **E1 → S0 → S1 → S3 → S4** и независимыми внешними
блокерами E2–E4 + юрист. Утверждения вида «40+ SP/день запаса» не используются —
SP разных потоков не складываются как человеко-часы в интеграционной неделе.

## Данные на staging (политика ПДн)

- Предпочтительно: **синтетические клиенты** (тестовые телефоны), маскированные
  реквизиты, реальные только названия услуг и расписания.
- Для настоящих мастеров: минимизация, ограниченный доступ, журнал доступа,
  срок удаления staging-данных, правовое основание отдельно, план очистки.
- **ИНН и реквизиты не попадают** в application logs, Sentry breadcrumbs,
  smoke-отчёты.

## DoD спринта (сводно)

1. Gates 0–4 пройдены с evidence (SHA, отчёты, метрики).
2. Coverage: unlinked = 0 по знаменателю пилотных мастеров.
3. Smoke SM-01…SM-09 зелёные + негативные сценарии зафиксированы.
4. Деньги: split/fee/подписка/dunning/pay-debt корректны (два раздельных
   сценария fee: online split / offline BookingFee).
5. Rollback rehearsal пройдена по процедуре.
6. Gate 5: ≥10 мастеров `active`, тексты юриста, go/no-go решение.
