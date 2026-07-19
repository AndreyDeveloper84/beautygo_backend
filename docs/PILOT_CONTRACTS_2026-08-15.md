# Волна 0 — Frozen Contracts пилота (2026-08-15)

**Contract version:** 1.5.0
**Frozen at:** 2026-07-18 (после пакета amendments READY-WITH-AMENDMENTS)
**Last amendment:** AMD-014 (2026-07-19, см. §14)
**Effective for pilot:** 2026-08-15
**Владелец:** оркестратор (Chief Product Architect); изменения — только amendment'ом (§13).

Единственный источник для стыков между потоками W1–W6. Агентам запрещено
«временно» придумывать поля, топики или endpoint'ы вне этого документа.

## 0. Нормативная иерархия

1. **Constitution** (`ayla-knowledge`) — продуктовые принципы, верховный уровень.
2. **Decision Log** (`ayla-knowledge/02 Strategy/`) — решения владельца (AYLA-DEC-*).
3. **Этот документ (Wave 0 Frozen Contracts)** — межпоточные технические контракты.
4. **ADR** (owning repos) — внутренняя реализация потока.
5. **Handoff / roadmap** — ненормативные описания задач.

Если ADR противоречит этому документу в части интерфейса между потоками —
применяется этот документ до formal amendment (§13).

## 1. Единый контракт данных (деньги, даты, ID)

Действует для C2–C5 и всех payload:

- Суммы — **строки Decimal**, всегда ровно два знака после запятой (`"5730.00"`).
- Валюта — **RUB**.
- Отрицательные значения запрещены (корректировки — только через amendment).
- Округление — **ROUND_HALF_UP**.
- Даты — ISO 8601 (`2026-08-01`); datetime — ISO 8601 **с timezone** (UTC).
- Идентификаторы — строки UUID; `event_id` — ULID.

## 2. C1. Billing Eligibility — можно ли принимать новую запись

**Producer:** W2 (`billing/`). **Consumer:** W1 (`appointments`, create path).
Форма — Python-вызов внутри репо (не HTTP).

```python
def can_accept_booking(
    specialist_id: UUID,
    tenant_id: UUID | None,
) -> EligibilityResult: ...

@dataclass(frozen=True, slots=True)
class EligibilityResult:
    ok: bool
    reason: Literal["SUBSCRIPTION_PAST_DUE"] | None = None
```

**Резолвинг billing account:**

- Запись в контексте салона (`tenant_id` передан, у tenant есть salon-подписка) →
  проверяется подписка салона; `past_due` салона блокирует новые записи
  **всех мастеров этого салона**.
- Самостоятельный мастер (`tenant_id` None или у tenant нет подписки) →
  личная подписка мастера.
- Мастер в нескольких организациях → проверяется контекст конкретной записи.
- Связь/данные не найдены или техническая ошибка → `ok=True` + Sentry alert
  (**fail-open в пилоте**); Sentry-запись без персональных и платёжных данных.

**Инварианты:**

- `ok=True` → `reason=None`; `ok=False` → `reason="SUBSCRIPTION_PAST_DUE"`.
- Ожидаемые billing-ошибки наружу не выбрасываются.
- Блокируется только создание **новой** записи. Существующие записи, перенос,
  отмена и завершение визита **не блокируются**.

**Приватность долга (обязательно):**

- Internal/backend получает код `SUBSCRIPTION_PAST_DUE` (HTTP 409).
- Мастер в своём кабинете видит экран погашения долга.
- Клиент получает **нейтральное** сообщение: «Сейчас запись к этому специалисту
  недоступна» + предложение другого мастера или времени.
- Клиентскому API причина задолженности **не передаётся** (generic UNAVAILABLE).

**Интеграция:** W1 вызывает в `CreateBookingService`; совместный
инвариант-тест W1×W2 обязателен (волна 3).

## 3. C2. Billing Status — статус подписки для бота/miniapp

**Owner:** W2. **Consumer:** W3 (master_api) → W4.

`GET /api/v1/internal/billing/specialists/{specialist_id}/status/` — Bearer `AYLA_INTERNAL_API_TOKEN`.

**200 (активная подписка):**
```json
{
  "data": {
    "specialist_id": "<uuid>",
    "subscription": {
      "status": "trial|active|past_due|canceled",
      "tariff": "solo|salon",
      "current_period_end": "2026-08-31",
      "next_charge": {
        "subscription_amount": "690.00",
        "fees_amount": "270.00",
        "total_amount": "960.00",
        "date": "2026-08-01"
      }
    },
    "fees": { "pending_total": "270.00", "pending_count": 3 },
    "last_invoice": { "id": "<uuid>", "amount": "960.00", "status": "paid", "paid_at": "2026-07-01T10:00:00Z" }
  }
}
```

**200 (нет подписки):**
```json
{
  "data": {
    "specialist_id": "<uuid>",
    "subscription": { "status": "none", "tariff": null, "current_period_end": null, "next_charge": null },
    "fees": { "pending_total": "0.00", "pending_count": 0 },
    "last_invoice": null
  }
}
```

**404** — только если специалист не существует или недоступен в tenant scope
(`SPECIALIST_NOT_FOUND`). Пустая выборка — всегда 200 с нулевыми значениями.

## 4. C3. Payout Preview — витрина выплаты мастеру

**Owner:** W1 (`payments/`). **Consumer:** W3 → W4.

`GET /api/v1/internal/specialists/{specialist_id}/payout-preview/` — Bearer internal.

**Состояния `capture_state`:**

| state | смысл | в pending_amount |
|---|---|---|
| `scheduled` | холд есть, capture запланирован (после визита) | ✅ входит |
| `captured_pending_settlement` | capture выполнен, ждёт выплаты ЮKassa | ✅ входит |
| `settled` | выплачено мастеру | ❌ |
| `capture_failed` | capture не удался | ❌ (инцидент reconciliation) |
| `canceled` | холд отменён | ❌ |
| `refunded` | возвращено клиенту | ❌ |

**Формула:** `pending_amount = sum(specialist_income)` по items со state
`scheduled` или `captured_pending_settlement`.

**200 (есть выплаты):**
```json
{
  "data": {
    "pending_amount": "5730.00",
    "currency": "RUB",
    "expected_settlement_hint": "~следующий рабочий день по расписанию ЮKassa",
    "items": [
      {
        "appointment_id": "<uuid>",
        "completed_at": "2026-07-18T16:00:00Z",
        "amount": "2000.00",
        "platform_fee": "90.00",
        "specialist_income": "1910.00",
        "capture_state": "scheduled"
      }
    ]
  }
}
```

**200 (пусто):** `{ "data": { "pending_amount": "0.00", "currency": "RUB", "expected_settlement_hint": null, "items": [] } }`
**404** — только если специалист не существует/недоступен.

**UI (W4):** различать два состояния явно:
- `scheduled` → «Ожидает подтверждения после визита»;
- `captured_pending_settlement` → «Подтверждено, ожидает перечисления».
Формулировки — «ожидается», никогда «гарантированно».

## 5. C4. Billing-события в eventbus

**Producer:** W2 (outbox). **Consumers:** W3 (уведомления мастеру), analytics.

**Envelope** (по event-contract, ADR-0009): `event_id` (ULID), `event_name`,
`event_version: 1`, `tenant_id`, `user_id`, `occurred_at` (ISO 8601 tz),
`correlation_id`, `payload`.

**Топики и payload:**

| event_name | payload |
|---|---|
| `subscription.activated` | `{specialist_id, tariff, period_end}` |
| `subscription.past_due` | `{specialist_id, debt_amount, failed_attempts}` |
| `billing.fee_charged` | `{specialist_id, appointment_id, amount, period}` |

**Идемпотентность и совместимость (обязательно):**

- Producer создаёт **стабильный `event_id`**; retry не создаёт новый event.
- Consumer дедуплицирует по `event_id`.
- Неизвестная `event_version` → **DLQ**, не молчаливое принятие.
- Неизвестные **дополнительные** поля payload игнорируются (forward compatibility);
  обязательные поля не могут отсутствовать.
- **Бизнес-инвариант:** одна appointment → не более одного `billing.fee_charged`;
  гарантия — `UNIQUE(appointment_id)` в `BookingFee`.

Имена топиков в `ALLOWED_EVENT_NAMES` добавляет W3; включение
`OUTBOX_EXTERNAL_DELIVERY_TOPICS` — решение и исполнение оркестратора.

## 6. C5. Personal Data Export/Delete (152-ФЗ)

**Цель:** acceptance-сценарий №6 — export и delete из miniapp.

### C5.1 Export

- **Ayla (W2):** `GET /api/v1/internal/users/{ayla_user_id}/personal-data/export/`
  → 200, **синхронный JSON**: profile subset + полный каталог personal-context
  (declared prefs). Bearer internal.
- **Bot (W3):** `GET /api/v1/customer/me/personal-data/export/` — агрегирует
  Ayla export + bot-side данные (MemoryEntry, consents) в один JSON
  (`Content-Disposition: attachment`). Асинхронные файлы/архивы — post-pilot.

### C5.2 Delete

- **Bot (W3):** `DELETE /api/v1/customer/me/personal-data/` — каскад:
  Ayla personal-context delete (endpoints W2), bot MemoryEntry delete/anonymize,
  consent withdraw cascade.
- **Идемпотентность:** повторный запрос — 200/204, не ошибка.
- **Audit:** запись (actor, timestamp, scope удаления) сохраняется
  **без удалённых персональных значений**.
- **Scope пилота:** personal context + память + consents. Транзакционные записи
  (записи, платежи) — retention по закону, обезличивание — post-pilot
  (явно вне этого контракта).

### Роли

- **W2:** Ayla-side export/delete endpoints.
- **W3:** агрегирующие customer-facing endpoints для W4.
- **W4:** UI «Мои данные» — запуск export (скачать JSON) и delete (подтверждение → статус).
- **W6:** dual-system smoke: создать данные → delete → проверить отсутствие
  в обоих backend.

## 7. R1. Напоминания о записи

- **Producer:** W1 — booking state/events через outbox (`booking.confirmed` и т.п.).
- **Owner доставки:** W3 — планирует и отправляет MAX-напоминания.
- **Пилотный сценарий:** один — **T−24h** до визита.
- **Идемпотентность:** `reminder_key = {appointment_id}:T-24h`; повторная
  доставка события не создаёт второе напоминание (dedupe по event_id + reminder_key).
- Отмена/перенос записи → отмена/перепланирование напоминания.

## 8. Владение общими файлами

| Общая зона | Владелец | Остальные |
|---|---|---|
| `djangoProject/settings/*`, `djangoProject/urls.py` | **W1** | W2 передаёт patch/требование через оркестратора |
| Celery beat schedule | **W1** | W2 пишет задачи в `billing/tasks.py`, запись расписания передаёт W1 |
| `users/models.py` (SpecialistProfile) | **W1** | W2 свои модели в `billing/` с FK — без правок `users/` |
| `payments/` | **W1** | W2 не редактирует |
| Bot API (`apps/master_api`, `apps/miniapp_api`, `apps/customer`) | **W3** | W4 потребляет только утверждённую схему |
| `apps/miniapp/src` (TypeScript) | **W4** | W3 не редактирует |
| Топики eventbus / `ALLOWED_EVENT_NAMES` | **W3** (по C4) | W2 передаёт имена через оркестратора |
| Этот документ | **оркестратор** | все — только чтение |

## 9. Границы пилота — post-pilot

Не реализуются, даже если в коде есть заготовки/TODO: аналитический дашборд,
onboarding салона, редактор persona, conversations (расширенные), маркетинговые
кампании, loyalty, earnings (расширенный), отпуска и замены мастеров, отзывы
(доработки), internal chat (доработки), offboarding, sleep tracker и любые
другие модули вне `PILOT_STREAMS_2026-08-15`.

**Правило:** всё вне брифа потока — post-pilot; нашёл заготовку — в отчёт, не в код.

## 10. Минимальные обязательные сценарии пилота (acceptance)

1. Запись без предоплаты: сообщение боту → подбор → запись → **напоминание T−24h (R1)** → complete.
2. Запись с онлайн-оплатой: hold → capture на complete → split 90₽ платформе / остальное мастеру (mock ЮKassa).
3. Мастер: привязка карты → списание подписки → инвойс и карточка «К выплате» (C2/C3).
4. Офлайн-запись: complete → BookingFee 90₽ начислен **ровно один раз** (AYLA-DEC-0010, C4).
5. Отмена записи → холд отменён автоматически.
6. 152-ФЗ: export и delete из miniapp (C5), dual-system проверка.
7. Память: бот задаёт вопрос → ответ сохранён → следующая рекомендация учла (consent-гейт).
8. Долг мастера → `past_due` → новая запись отклонена по C1; клиент видит нейтральное сообщение.

## 11. Baseline (2026-07-18; перепроверить при fetch)

Канонические имена ↔ локальные пути:

| Репо (канон) | Локальный путь | Ветка | SHA | Расхождение |
|---|---|---|---|---|
| beautygo_backend | `…\Ayla\djangoproject` | `feat/memory-foundation-internal-api` | `f6e9572e` | +2/−9 к dev |
| ai-bot-platform | `…\ai-bot-platform` | `feat/memory-consent-global` | `fe6c1f8` | +1/−20 к origin/dev |
| ayla-ai-core | `…\ayla-ai-core` | `feat/memory-context-builder` | `b8b8072` | +1/−2 к main |
| frontAyla (= `Shiro-Py/frontbeauty`) | `…\Ayla\frontAyla` | `dev` | `5f9e31e` | этап 2 |
| ayla-knowledge | `…\Ayla\ayla-knowledge` (+ worktree `ayla-knowledge-agent`) | `main` | `d24d280` | Decision Log: ветка `agent/decision-log` (`dc25045`, review) |

## 12. Тестовые команды (исполняемые буквально)

| Репо | Команда |
|---|---|
| beautygo_backend | `pytest --ds=djangoProject.settings.test` |
| ai-bot-platform | `uv run pytest -m "not smoke"` |
| ayla-ai-core | `pytest` |
| miniapp (`apps/miniapp`) | `npm ci && npm run typecheck`; unit-тесты — vitest добавляет W4, затем `npm test -- --run` |
| ayla-knowledge | `python scripts/validate_knowledge.py && python -m unittest discover -s tests -v` |

## 13. Amendment procedure

Каждый amendment к этому документу содержит:

1. номер amendment;
2. причину;
3. изменённый контракт (C1–C5/R1);
4. затронутые потоки;
5. миграционное действие (что должен сделать каждый поток);
6. дату вступления в силу;
7. ссылку на решение владельца (Decision Log, AYLA-DEC-*).

Amendment оформляется веткой + commit в `beautygo_backend/docs/` и bump
**Contract version** (MAJOR — несовместимое; MINOR — аддитивное).


## 14. Amendment Log

### AMD-001 — C6: Catalog Link Contract (2026-07-18, MINOR)

- **Причина:** W1/W3 нужен единый матчинг услуг для `link_ayla_service_ids`;
  у `ServiceTemplate` нет поля slug. Решение оркестратора: поле **не добавляем**
  в пилоте (нет миграций модели ради матчинга).
- **Контракт C6:** матчинг по паре (`category_slug`, нормализованное `name`) +
  duration как tiebreaker. Нормализация: lower, trim, ё→е, схлопывание пробелов,
  удаление кавычек-ёлочек. Команда поддерживает mapping file исключений
  (ручные соответствия). Отчёт покрытия: `matched auto` / `matched manual` /
  `unmatched`.
- **W1-сторона:** internal specialist-services mirror отдаёт `template_id` +
  `name` + `category_slug` + duration (документ `docs/CATALOG_INTERNAL_API_CONTRACT.md`
  §3; реализовано, коммит `e988dfb9`).
- **Потоки:** W1 (сделано), W3 (link — в работе).
- **Миграционное действие:** нет (аддитивно).
- **Решение:** оркестратор, ответ W1 от 2026-07-18; кандидат AYLA-DEC-0011
  при следующем батче Decision Log.

### AMD-002 — #1016 MINOR: `payment_required` на create (2026-07-18, MINOR)

- **Причина:** запись без предоплаты (D6) требует опционального флага.
- **Контракт:** поле `payment_required` (bool, default `true`) на клиентском и
  internal create; `false` → без Payment, сразу CONFIRMED + событие
  `booking.confirmed`. Обратная совместимость сохранена. Эталон сверки —
  route-table тест бота.
- **Потоки:** W1 (сделано, `24a340a4`), W3 (клиент REST), W4 (UI выбора оплаты).
- **Решение:** AYLA-DEC-0006.

### AMD-003 — C1: путь модуля eligibility (2026-07-18, MINOR)

- **Контракт:** `can_accept_booking` живёт в `billing/services.py`
  (пакет `billing.services`). W1-адаптер импортирует оттуда.
- **Потоки:** W1 (реализовано), W2 (**подтвердить этот путь** при реализации).
- **Решение:** оркестратор (по запросу W1).

### AMD-004 — Flat-fee edge: услуга дешевле fee (2026-07-18, MINOR)

- **Контракт:** `platform_fee = min(90.00, price)`;
  `specialist_income = max(0, price − fee)`. Отрицательные значения запрещены
  (§1). В пилот-каталоге цен < 90₽ нет; правило зафиксировано кодом и тестом W1.
- **Потоки:** W1 (сделано), W2 (учесть в BookingFee при аналогичном edge).
- **Решение:** AYLA-DEC-0001.


### AMD-005 — Ключ мастера в C2/C3/C4: Ayla User UUID (2026-07-18, MINOR)

- **Причина (находка W3, Q2):** `SpecialistProfile.id` — отдельный uuid4, бот
  зеркалит мастеров по **Ayla User UUID**. Контракты C2/C3/C4 ключились на
  `specialist_id` — двусмысленно.
- **Контракт (уточнение):** во всех internal billing/payout endpoints и
  billing-событиях ключ мастера = **Ayla User UUID** (`user_id`), НЕ
  `SpecialistProfile.id`. Резолвинг user → SpecialistProfile — внутри
  W1 (payout, C3) и W2 (billing, C2/C4). Согласовано с frozen personal-context
  contract (там `ayla_user_id` в путях).
- **Потоки:** W1 (резолвер в payout preview), W2 (модели/endpoint'ы ключовать по
  user), W3 (снимает fail-closed 503 после реализации).
- **Решение:** оркестратор (по эскалации W3); вариант «enrichment sync поля на
  CatalogMaster» отклонён — лишняя синхронизация в пилоте.

### AMD-006 — C5.2: upstream DELETE path (2026-07-18, MINOR)

- **Контракт:** upstream delete на стороне Ayla —
  `DELETE /api/v1/internal/users/{ayla_user_id}/personal-data/`
  (симметрично export C5.1). Идемпотентно (повтор → 200/204).
- **Потоки:** W2 (реализовать именно по этому пути), W3 (клиент — уже принял).
- **Решение:** оркестратор (по запросу W3 Q4).

### AMD-007 — C4: полный envelope по event-contract (2026-07-18, MINOR)

- **Причина (W3 Q5):** в PILOT_CONTRACTS §5 указана только payload; бот требует
  полный формат конверта.
- **Контракт (уточнение):** envelope C4-событий — строго по
  `ai-bot-platform/docs/architecture/event-contract.md` (ADR-0009):
  `event_id` (ULID), `event_name`, `event_version`, `tenant_id`, `user_id`,
  `occurred_at`, `correlation_id`, `causation_id`, `actor`, `data`.
  Поля payload из §5 этого документа — содержимое `data`.
- **Потоки:** W2 (эмитить в полном формате), W3 (consumers — готовы).
- **Решение:** оркестратор (по запросу W3 Q5).


### AMD-008 — C4: `event_id` = UUID4, не ULID (2026-07-18, MINOR)

- **Причина (W2 R-3):** фактический код использует UUID4 (`event_id` = PK
  `OutboxEvent`, UUIDField) — задокументированное отклонение от ADR-0009 в
  `appointments/infrastructure/outbox/envelope.py`.
- **Контракт (уточнение):** `event_id` — строка UUID4. Consumer дедуплицирует по
  `event_id` как строке. Переход на ULID — post-pilot, при реальной необходимости.
- **Потоки:** W2 (эмитит), W3 (consumers — уже дедупят по строке).
- **Решение:** оркестратор (по эскалации W2).

### AMD-009 — AYLA-DEC-0010: предикат онлайн-оплаты (2026-07-18, MINOR)

- **Контракт (уточнение инварианта одиночного взыскания):** `BookingFee`
  начисляется, если по записи **не существует** `Payment` со статусом в
  `{authorized, paid}`. Платежи в `failed`/`pending` (брошенные) онлайн-оплатой
  не считаются. `paid` с последующим полным refund fee не порождает (split был,
  refund обратный).
- **Потоки:** W2 (реализует предикат), W1 (совместный инвариант-тест, волна 3).
- **Решение:** оркестратор (по предложению W2, подтверждено).

### AMD-010 — C5.2: audit удаления через AnalyticsEvent (2026-07-18, MINOR)

- **Контракт:** audit-запись удаления персональных данных — через
  `AnalyticsEvent` (прецедент: revoke-аудит tenants; emit-хелперы
  `users/personal_context_events.py`), **без персональных значений** (actor,
  timestamp, scope). Отдельную audit-модель в пилоте не создаём.
- **Потоки:** W2 (реализует).
- **Решение:** оркестратор (по предложению W2, подтверждено).


### AMD-011 — Предикат онлайн-оплаты: +refunded (2026-07-19, MINOR)

- **Причина (W6 D-1):** W2 реализовал предикат AMD-009 шире контракта — включил
  `refunded`/`partially_refunded` в множество «была онлайн-оплата». Ревизией
  принято как более корректное: если клиенту вернули деньги, взыскивать fee с
  мастера нельзя.
- **Контракт (заменяет AMD-009):** `BookingFee` начисляется, если по записи
  **не существует** `Payment` со статусом в
  `{authorized, paid, refunded, partially_refunded}`.
  Платежи в `failed`/`pending` онлайн-оплатой не считаются.
- **Потоки:** W2 (уже реализовано — соответствует), W1 (совместный тест — обновить).
- **Решение:** оркестратор (по матрице W6).

### AMD-012 — R1: второе напоминание T−2h (2026-07-19, MINOR)

- **Причина (W6 D-2):** бот уже планирует напоминание T−2h сверх контрактного
  T−24h (существующее поведение dev). Удалять работающее — дороже, чем принять.
- **Контракт (уточнение R1):** пилотные напоминания — **T−24h (обязательное) +
  T−2h (допустимое, оставлено)**. `reminder_key = {appointment_id}:{offset}` —
  дедуп отдельно по каждому offset; отмена/перенос записи отменяет/переносит
  оба. Дополнительных offsets в пилоте не вводить.
- **Потоки:** W3 (уже работает — соответствует), W6 (smoke: оба offset'а, без дублей).
- **Решение:** оркестратор (по матрице W6).


### AMD-013 — C2: семантика `next_charge.date` (2026-07-19, MINOR)

- **Причина (W2):** пример в C2 был внутренне противоречив (`current_period_end`
  08-31, `next_charge.date` 08-01).
- **Контракт (уточнение):** `next_charge.date` = `current_period_end + 1 день`
  (день продления, charge-in-advance — списание подписки за следующий период).
  Для `status: canceled` → `next_charge: null`. Пример в C2 считать устаревшим.
- **Потоки:** W2 (уже реализовано), W3/W4 (отображение: «следующее списание»).
- **Решение:** оркестратор (по запросу W2).

### AMD-014 — Billing webhook: AppType-exempt path (2026-07-19, MINOR)

- **Причина (W2):** YooKassa server-to-server не может передать заголовок
  `X-App-Type` — webhook под AppTypeMiddleware получил бы 403.
- **Контракт:** billing webhook —
  `POST /api/v1/internal/billing/webhook/` (префикс AppType-exempt).
  Безопасность: IP allowlist + Basic Auth (по образцу payments webhook).
  Настройка webhook URL в кабинете ЮKassa — задача W6/оркестратора при
  staging-фазе.
- **Потоки:** W2 (уже реализовано), W6 (чеклист staging), W1 (см. P8 — та же
  проверка для payments webhook).
- **Решение:** оркестратор (по запросу W2).
