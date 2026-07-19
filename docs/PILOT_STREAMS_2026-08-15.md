# Пилот 2026-08-15 — карта стримов и брифы для окон

Дата: 2026-07-18. Основание: Decision Log (PROJECT_INDEX.md §5.4), MVP Roadmap 2026-07, аудит payments от 2026-07-18.
Осталось **4 недели**. Пилот: Пенза, MAX-бот + MAX Mini App; онлайн-оплата клиентом опциональна (YooKassa split per-master, 90₽ платформе при капче); подписка мастера — автосписание (рекуррент); внутренний баланс/кошелёк — этап 2 (D8).

Правила для всех окон:
- Работаем только в своём scope; чужие приложения/модули не трогаем.
- Каждое окно — своя feature-ветка от актуального `dev` (после sync).
- Перед стартом: `git fetch --all`, rebase/merge на свежий `dev` (у W1 −9, у W3 −20, у W5 −2 от main).
- Контрактные изменения между репо (events, internal API) — только через согласование с оркестратором, с обновлением контрактов в docs/.
- **Контракт перед интеграцией:** любой стык между окнами (events, internal API, billing-топики, booking REST) сначала замораживаем минимальным контрактом в docs/ (как Personal Context API v1.0), потом пишем код. Нарративные документы (PRD, спеки доменов) могут догонять код; контракты — нет.
- Тесты обязательны: новая логика без тестов не принимается.

---

## W1 — Ayla Booking Core (djangoproject)

**Цель:** бот может полностью вести бронирование через Ayla REST; запись подтверждается без платежа.

Scope:
1. Rebase `feat/memory-foundation-internal-api` на свежий `dev` (9 коммитов YClients intake, #205–#215) — конфликты по каталогу решаем первым делом.
2. **Онлайн-оплата клиентом (D6/D8/D9):** fix capture — hold→capture на `complete()` (сейчас `capture_payment` не вызывается нигде в продовом коде — критический баг); отмена записи → автоматическая отмена холда через YooKassa `/cancel` (метода нет в коде — добавить); комиссия **flat 90₽** вместо 8% (`payments/services.py:81-85` + `appointments/domain/policies.py:96`); split **per-master** — transfers на суб-счёт конкретного мастера (сейчас один общий sub-account); запись без предоплаты сохраняется — онлайн-оплата опциональна, оба пути CONFIRMED должны работать.
   - **Capture (D9, ADR `docs/architecture/payments-capture-strategy.md`):** `CAPTURE_DELAY_HOURS=0` в пилоте; механизм — параметризуемая отложенная задача; `capture_at = min(completed_at + delay, expires_at − 60m)` (срок capture берём из `expires_at` ЮKassa, не из константы «7 дней»); уникальный idempotency key, retry с backoff, периодический reconciliation job, алерты (платёж приближается к `expires_at`; `completed`-визит застрял в `waiting_for_capture`), ручная management-команда повтора capture; обработка webhook. UX-статусы: клиент — `PAYMENT_AUTHORIZED/CAPTURE_SCHEDULED/CAPTURED/RELEASED` («зарезервировано/будет подтверждена/завершена/разблокировано»), мастер — «Ожидает визита → Готово к списанию → Подтверждена → К выплате → Выплачено»; `waiting_for_capture` пользователям не показывать.
3. S1-A: домёржить #201 (canonical seed); internal API отдаёт `template_id`/`slug` боту (#200 t5).
4. Верифицировать контракт слотов/брони для бота (#1016 S2: create/cancel/reschedule, миррор каталога) против `tests/contracts/`.
5. **Payout preview (витрина выплаты):** internal endpoint для бота — сумма к выплате мастеру: `sum(specialist_income)` по платежам «холд/капча» за период, разбивка по записям (2000₽ − 90₽ = 1910₽), ожидаемая дата («~следующий рабочий день по расписанию ЮKassa» — формулировка «ожидается», не «гарантированно»). Read-only, данные уже в `payments/models.py`.

DoD: бот через `AYLA_INTERNAL_API_TOKEN` делает create/cancel/reschedule; онлайн-оплата: hold→capture на `complete()` со split 90₽ per-master, отмена записи автоматически отменяет холд (mock ЮKassa в тестах); `pytest` зелёный; e2e-smoke (с W6) проходит.

## W2 — Ayla Billing & Legal (djangoproject, новое приложение `billing/`)

**Цель:** деньги по модели D1/D7: подписка 690/990₽ + 90₽ за успешную запись, автосписание.

Scope:
1. Модели: `TariffPlan` (solo 690 / salon 990), `SpecialistSubscription` (status, current_period, `payment_method_id`), `BookingFee` (90₽, FK appointment, начисление на `complete()` — **только для записей без онлайн-оплаты**; по онлайн-оплаченным 90₽ уже удержан split'ом в W1), `BillingInvoice`, `BillingPayment`. Приложение `billing/` — `payments/` расширяет W1 (split per-master).
2. Первый платёж мастера: YooKassa `save_payment_method: true` → confirmation_url → webhook `payment.succeeded` → сохранить `payment_method_id`. Идемпотентность (прецедент: `X-Idempotency-Key` fallback).
3. Beat-задача (monthly): рекуррентное списание = подписка + сумма BookingFee за период; `payment_method_id` + идемпотентный ключ. Dunning: fail → retry ×3 (T+1d, T+3d) → `debt` → **блокировка новых записей** (hook в booking create, через W1-контракт).
4. Чеки 54-ФЗ: услуга платформы мастеру (переработать receipt-builder под плательщика=мастер).
5. Согласие на автоплатёж: текст оферты + фиксация consent (кому: legal; где хранить — `BillingConsent`).
6. Internal API для бота: статус подписки/баланс/инвойсы мастера (bot → master_api → Mini App экран биллинга, W4).
7. События в outbox: `subscription.activated`, `subscription.past_due`, `billing.fee_charged` — топики согласовать с W3 (ALLOWED_EVENT_NAMES).
8. Legal: wiring 152-ФЗ endpoints personal-context (код в `personal_context_views` есть, не подключён — S3.1).

DoD: мастер привязывает карту, списывается подписка, fee начисляется на completed-записи, при долге запись блокируется; 152-ФЗ endpoints доступны; тесты на все денежные пути.

## W3 — Bot Backend (ai-bot-platform)

**Цель:** каталог связан, гейт снят, память персистится, события ходят в обе стороны.

Scope:
1. Sync с `origin/dev` (−20 коммитов!) — сначала.
2. S1-B: `link_ayla_service_ids` + прогон + отчёт покрытия (порог ~100% для Пензы) → домёржить #1045 (hardening) → подготовить флип #1041 / снятие гейта `BOOKING_VIA_AYLA_REST`.
3. Booking через Ayla REST: RemoteBookingProxy против контракта W1; убрать stub-режим booking (совместно с W4 по #856).
4. Eventbus round-trip: включить `OUTBOX_EXTERNAL_DELIVERY_TOPICS` на стороне Ayla (совместно с W1) + проверить ingest `/api/v1/internal/events/ingest` (идемпотентность, DLQ). Новые billing-топики от W2 — добавить в ALLOWED_EVENT_NAMES.
5. Memory (S3-B): persistence inferred-памяти, интеграция consent cascade (`feat/memory-consent-global` — домёржить), вызовы `should_ask/mark-asked/skip` (Ayla internal API, frozen contract v1.0).
6. Billing-proxy: прокидывать статус биллинга мастера (W2 internal API) в master_api.
7. Payout preview proxy: прокидывать витрину выплаты (W1 internal API) в master_api дашборда мастера.

DoD: покрытие каталога 100%, гейт снят на staging, booking e2e без stub, события booking.*/payment.* доезжают в бота, memory-вопросы работают в DM.

## W4 — Mini App FE (ai-bot-platform/apps/miniapp)

**Цель:** юридический блокер закрыт, booking-flow на реальном API, базовые тесты.

Scope:
1. **152-ФЗ (юр-блокер пилота):** шторки export + delete в customer-profile → endpoints W2/W1 (S3.1); support deeplink (#949).
2. Booking-flow un-stub (#856): после W3 — реальные слоты/создание/перенос/отмена через bot API (без платёжного экрана — D6).
3. Экран биллинга мастера: статус подписки, привязка карты (через confirmation_url W2), инвойсы. Карточка **«К выплате»** в дашборде: сумма (5730₽ в примере), разбивка по записям, «ожидается ~завтра (по расписанию ЮKassa)».
4. Consent на автоплатёж (текст от W2/legal) в флоу привязки карты.
5. Профиль #946–953, notification-prefs (#948).
6. Фронт-тесты с нуля: booking, consent, billing (критичные флоу).

DoD: export/delete работают, booking e2e на staging, billing-экран живой, `tsc` чистый, тесты критичных флоу зелёные.

## W5 — AI-core → Concierge (ayla-ai-core, затем ai-bot-platform)

**Цель:** релиз памяти в ai-core, консьерж реально использует память в диалоге.

Scope:
1. Rebase `feat/memory-context-builder` на `main`; экспорт `build_memory_block` из `__init__.py` + snapshot-тест + CHANGELOG; релиз (0.9.0 или 0.8.2 — по RELEASING.md); **парный бамп SHA в djangoproject и ai-bot-platform**.
2. S2: wiring консьержа в боте (DRF-241): диалог → подбор → бронь сквозь Ayla REST (после W1/W3).
3. S3.5: консьерж вызывает `should_ask_question` и органично задаёт вопрос в DM (source 1 end-to-end); инъекция memory-блока в промпт (с учётом consent-гейта memory_green — бот, W3).
4. Верификация booking tools + anti-hallucination на реальном каталоге.

DoD: оба backend на новом SHA; сквозной сценарий «вопрос → ответ → память обновилась → рекомендация учла память» работает в staging.

## W6 — QA / Docs / Runbook (кросс-репо, непрерывно)

**Цель:** стыки не разъезжаются, документы соответствуют решениям, пилот готов к запуску.

Scope:
1. Документы (неделя 1): ребейз `MVP_ROADMAP_2026-07.md` под D1–D7 (даты, канал MAX, pricing, без онлайн-оплаты) + синк зеркала в Vault; Killer PRD v1.1 skeleton (память в центре, еда — триггер).
2. E2E smokes: miniapp→bot→Ayla (booking CRUD), memory-ask flow, billing charge flow (mock YooKassa), eventbus round-trip.
3. Контрактные тесты между репо (route-table, events schema).
4. Pilot-readiness runbook: deploy, rollback, dual-system delete (#937), канарейка, Concierge Mode чеклист (первые 100–500 пользователей — ручная проверка рекомендаций по Journey Spec).
5. Синк веток: еженедельный контроль drift'а окон (fetch + status по всем репо).

DoD: runbook принят, smokes в CI, документы зеркалены в Vault, freeze-чеклист готов к неделе 4.

---

## Волны

- **Неделя 1 (18–25.07):** все — sync/rebase. W1: booking без оплаты + каталог. W3: link IDs. W5: релиз ai-core. W2: модели billing. W4: 152-ФЗ шторки. W6: ребейз документов.
- **Неделя 2 (25.07–01.08):** W3: флип + booking REST. W2: первый платёж + рекуррент. W5: concierge wiring. W4: billing-экран. W6: e2e scaffolding.
- **Неделя 3 (01–08.08):** W4: un-stub booking. W2: dunning + блокировка. W5: memory-ask в DM. W3: eventbus + consent cascade. W6: полный прогон e2e.
- **Неделя 4 (08–15.08):** стабилизация, канарейка, runbook-репетиция, **заморозка фич 12.08**, Concierge Mode настройка. Пилот 15.08.

## Критический путь

sync веток → S1 (каталог + booking REST без оплаты) → флип гейта → un-stub miniapp → e2e. Параллельно идёт billing (W2) и memory (W5) — обе должны закрыться к неделе 3.

## Риски оркестрации

- W1 и W2 делят репо — граница: W1 трогает `appointments/`, `services/`, `payments/` (флаг), W2 — только новый `billing/` + `users/personal_context_views`. Общие файлы (`settings/`, `urls.py`) — мёрж через dev, W6 следит.
- Рекуррент требует привязки карты мастером через confirmation_url в браузере — известная фрикция MAX (YooKassa вне MAX). Для пилота приемлемо, зафиксировать в runbook.
- **KYC-онбординг мастеров в ЮKassa** (суб-счета для split per-master) — операционный процесс: договор + данные мастера/самозанятого. Стартовать на неделе 1 (W6 + юрист), иначе split не заработает на живых мастерах к неделе 3.
- Юридика автоплатежа (оферта + согласие) и агентская формулировка в чеках split (чек клиенту от имени мастера) — нужен текст от юриста до конца недели 2, иначе W1/W2 встают.
