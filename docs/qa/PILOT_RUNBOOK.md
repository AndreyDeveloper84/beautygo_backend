# PILOT RUNBOOK — 2026-08-15 (Пенза, MAX-бот + MAX Mini App)

**Поток:** W6 · **Версия:** 2026-07-19 · **Основание:** PILOT_CONTRACTS v1.8.0, PILOT_STREAMS,
Decision Log (AYLA-DEC-*), фактические deploy-артефакты репо (deploy.sh, entrypoint, compose).
**Окружения:** staging = `dev.gobeauty.site` (Ayla) + bot staging · prod — пост-пилот.

## 0. Артефакты и порядок чтения

- Smoke-runner: `scripts/pilot_smoke/` (README + S1–S7) — прогон после каждого deploy и flip.
- Контрактная матрица: `docs/qa/CONTRACT_MATRIX_2026-08-15.md` (статусы стыков, блокеры).
- Тест-команды репо: Contracts §12. Перед любым flip — `pytest` зелёный на обоих backend.

## 1. Deploy / Rollback

### 1.1. Порядок (оба backend): **флаги OFF → миграции → код → флаги ON**

1. Убедиться, что новые флаги выключены (таблица §3).
2. **Ayla первым** (internal API аддитивен, бот зависит от его контрактов):
   ```bash
   cd /home/taximeter/beautygo/dev
   git fetch origin dev && git reset --hard origin/dev
   # миграции отдельным шагом ДО поднятия нового кода:
   docker compose run --rm web python manage.py migrate --noinput
   docker compose build && docker compose up -d
   docker compose ps && curl -sf http://127.0.0.1:8000/api/docs/   # health
   ```
   (entrypoint web-контейнера сам делает `migrate --noinput` → `collectstatic` → gunicorn;
   явный шаг миграций — для контролируемого окна и отката БД при failure.)
3. **Bot вторым** (staging, `config/settings/staging.py`, `DEBUG=False`): обновить `dev`,
   пересобрать, поднять web/worker/beat; health — ingest liveness (401 на неподписанный POST =
   жив и закрыт).
4. Флаги — только после зелёного smoke (`scripts/pilot_smoke`, все PASS/SKIP-объяснены).

### 1.2. Rollback

1. **Сначала флаги назад** (§3, колонка «rollback») — это снимает новое поведение без даунтайма.
2. Код (если флагов недостаточно):
   ```bash
   git reset --hard <prev-sha> && docker compose build && docker compose up -d
   ```
3. Миграции пилота (§2) — **аддитивные** (новые колонки/таблицы, nullable) → откат кода БЕЗ
   отката БД безопасен. `billing/0002` (seed тарифов) обратим.
4. После rollback — smoke S1/S4 как минимум.

## 2. Staging-миграции (пилотный пакет)

| Миграция | Содержание | Тип |
|---|---|---|
| `appointments/0012_appointment_completed_at` | поле `completed_at` (D9 capture scheduling, C3) | add, nullable |
| `appointments/0013_alter_outboxevent_topic` | choices + billing-топики (`subscription.*`, `billing.fee_charged`) | alter choices (без DDL) |
| `payments/0003_payment_capture_lifecycle` | `capture_state`, `capture_scheduled_for`, `yookassa_expires_at`, `captured_at` | add, nullable |
| `users/0014_specialistprofile_yookassa_account_id` | суб-счёт мастера (split per-master, D8) | add, nullable |
| `billing/0001_initial` | 6 моделей W2 (TariffPlan, SpecialistSubscription, BookingFee, BillingInvoice, BillingPayment, BillingConsent) | create |
| `billing/0002_seed_tariff_plans` | seed solo 690.00 / salon 990.00 | RunPython, обратим |

Проверка после migrate: `python manage.py showmigrations appointments payments users billing`.

## 3. Флаги и конфиг (staging preflight — закрыть ДО smoke)

| # | Ключ | Где | Default | Действие для staging | Rollback |
|---|---|---|---|---|---|
| F0 | **`EVENT_INGEST_HMAC_SECRET`** | bot settings | getattr-only, **из env не загружается** (config-gap) | загрузить из env/secret-store; без этого ingest = 401 `no_secret` на ВСЁ | — |
| F1 | `AYLA_OUTBOUND_HMAC_SECRET` | Ayla env | `""` | = F0 (пара) | — |
| F2 | `BOT_PLATFORM_BASE_URL` / `BOT_PLATFORM_INGEST_PATH` | Ayla env | `""` / `/api/v1/internal/events/ingest` | URL bot staging | — |
| F3 | `OUTBOX_EXTERNAL_DELIVERY_TOPICS` | Ayla env | `""` (ничего не уезжает) | ступенями: `booking.created,booking.confirmed` → +`booking.cancelled,booking.rescheduled` → +billing-топики (D-3, решение оркестратора) | очистить |
| F4 | `BOOKING_VIA_AYLA_REST` | bot env | `false` | flip #1041 ТОЛЬКО после coverage-отчёта каталога (Пенза ~100%) | `false` |
| F5 | `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY` | Ayla env | `""` (503 на платёжных путях) | **test-shop** креды на staging | — |
| F6 | `YOOKASSA_WEBHOOK_ALLOWED_IPS` (+ optional Basic) | Ayla env | `[]` (accept-all + warning; prod fail-fast) | CIDR ЮKassa на staging; Basic — по возможности | — |
| F7 | `CAPTURE_DELAY_HOURS` | Ayla env | `0` | 0 (D9); 24 — только по измеримым триггерам | `0` |
| F8 | `MAX_BOT_TOKEN`, `MAX_BOT_TENANT_SLUG` | bot env | — | staging-бот | — |
| F9 | `NUTRITION_ENABLED` | bot env | `false` | по готовности food-flow (вне критпути) | `false` |
| F10 | `MULTI_TENANT_STRICT` | Ayla env | `false` | `true` + `X-Tenant` после проверки tenant-данных | `false` |
| F11 | `CONCIERGE_MEMORY_ENABLED` | bot env | `true` | `true` (default; гасит всю memory-поверхность консьержа: блок + memory-ask; диалог работает) | `false` |

Каталог/данные: `seed_canonical_catalog`, `seed_service_templates`, `seed_regional_pricing`,
`backfill_tenants`; тарифы — миграцией `billing/0002`. Расписание/слоты мастеров — через админку
(для smoke нужен ≥1 активный специалист с услугой и слотами на +7 дней).

## 4. ЮKassa webhook URL (кабинет ЮKassa)

Два независимых приёмника на Ayla (оба AppType-exempt):

| Webhook | URL | События | Контракт |
|---|---|---|---|
| payments (клиентские платежи) | `https://<host>/api/v1/payments/webhook/` | `payment.waiting_for_capture`, `payment.succeeded`, `payment.canceled`, `refund.succeeded` | после P8-fix (403 через AppTypeMiddleware устранён) |
| billing (подписка мастера) | `https://<host>/api/v1/internal/billing/webhook/` | `payment.succeeded`, `payment.canceled` | AMD-014 |

Шаги: кабинет test-shop (staging) → HTTP-уведомления → прописать оба URL → проверить тестовым
событием (ожидание: 200 `{"status":"ok"}`; повтор — `duplicate`). Без F6 приёмник работает, но
открыт — для прода allowlist обязателен (prod fail-fast). Повторная доставка от ЮKassa — норма,
идемпотентность по `object.id`.

## 5. Канарейка (неделя 4, 08–12.08)

**Вход (все обязательны):**
- [ ] smoke S1–S7 на staging: 0 FAIL, SKIP — с объяснениями (отчёт приложен).
- [ ] Coverage-отчёт `link_ayla_service_ids` на staging ≥ порога (Пенза ~100%) → flip F4.
- [ ] 8 acceptance-сценариев §10 пройдены на staging вручную/полуавтоматом (что не покрыто smoke).
- [ ] Юр. тексты готовы (оферта автоплатежа, агентская формулировка чеков — дедлайн 01.08).
- [ ] KYC ≥ канареечных мастеров в ЮKassa (суб-счета, иначе split не работает — 422 на онлайн-оплате).
- [ ] Webhook'и §4 настроены и проверены тестовыми событиями.

**Объём:** 1 tenant, 3–5 мастеров, внутренние пользователи (команда + фаундер).
**Наблюдение (ежедневно):** `capture_failed`, Sentry-алерты reconciliation, ingest DLQ
(`eventbus_ingestdlq` пуст), dunning-переходы, жалобы Concierge Mode (§7).
**Триггеры отката:** любой capture-инцидент с деньгами; DLQ > 0 по booking.*/billing.*;
mass FAIL smoke; safety-нарушение консьержа → §1.2 (флаги → код).

## 6. Заморозка 12.08 (feature freeze)

**Замораживаем:** миграции схемы; контракты (изменения — только amendment §13); новые флаги;
FE-релизы miniapp (кроме fix); новые топики событий; депенденси-бампы (ai-core SHA и пр.).
**Разрешено:** bugfix с тестом + review (минимальный дифф); конфиг/секреты/лимиты; документы;
данные каталога (услуги/мастера Пензы) через штатные команды.
**Процесс:** 12.08 — прогон полного smoke + ручные acceptance; 13–14.08 — runbook-репетиция
(deploy/rollback по §1 на staging), канарейка расширяется; 15.08 — пилот.

## 7. Concierge Mode чеклист (первые 100–500 пользователей)

Ручная проверка рекомендаций консьержа по Journey Spec. Каденс: первые 100 юзеров — выборка
100% диалогов с рекомендацией ежедневно; 100–500 — ≥20% ежедневно.

- [ ] **Boundary safety:** нет медицинских утверждений/диагнозов/обещаний результата;
  гипотезы — вероятностным языком; при риске — эскалация к специалисту (Constitution ст. VIII, XII).
- [ ] **Объяснимый ranking:** у каждой рекомендации виден источник («потому что вы сказали…»);
  факты — только из памяти с consent; платный статус мастера НЕ влияет (ст. IV).
- [ ] **Consent-гейт:** без `memory_green` — нет should_ask-вопросов и нет записи фактов;
  skip не приводит к повтору вопроса в cooldown.
- [ ] **Проактивность в меру (ст. X):** ≤2 вопросов/нед, жёсткий отказ → тема закрыта.
- [ ] **Rollback готов:** выключатель консьержа/memory-ask известен дежурному **[TBD W5:
  имя env-флага — подтвердить до 12.08]**; коммуникация пользователям на случай отключения готова.
- [ ] Журнал проверок: дата, выборка, нарушения, действие. **Любое safety-нарушение → стоп
  + обязательный разбор (ст. XIII), возврат только после фикса причины.**

## 8. Инциденты

### 8.1. Холд сгорел / capture застрял
- Автоматика: `reconcile-captures` (каждые 5 мин) лечит `completed_stuck`, алертит
  `expiry_approaching` (2×buffer до `yookassa_expires_at`) и `capture_failed`.
- Ручное: `docker compose exec web python manage.py retry_capture [--payment-id <uuid>] [--sync]`
  (идемпотентно, ключ `capture-{payment.id}`).
- Холд сгорел (`expires_at` прошёл): деньги разблокированы у клиента, платёж → `canceled`;
  клиенту — «резерв отменён»; запись идёт как офлайн → fee 90₽ через billing (инвариант AMD-011).
  Повторную оплату — новой записью/платежом, НЕ «оживлением» старого холда.

### 8.2. Refund вручную
- Штатно: клиентский `POST /api/v1/payments/{id}/refund/` `{amount?}` (default — полный net).
- Shell: `YooKassaService.refund_payment(provider_payment_id, amount, idempotency_key)`.
- Проверка: `GET /api/v1/payments/{id}/` → `refunded`. Полный refund не порождает BookingFee (AMD-011).

### 8.3. Мастер заблокирован по ошибке (past_due)
1. Проверить причину: `GET /api/v1/internal/billing/specialists/{user_uuid}/status/` + инвойсы.
2. Снятие: `/admin/billing/specialistsubscription/` → `status=active`, `failed_attempts=0`,
   `next_retry_at=null`, проверить `current_period_end`; при ошибочном инвойсе — переоткрыть/аннулировать.
3. Верификация: C2 → `active`; тестовый create booking проходит (C1 fail-open не требуется).
4. Зафиксировать причину в журнале (dunning T+1d/T+3d — не отключать глобально).

### 8.4. Outbox / ingest
- Ayla dead-события: `python manage.py replay_dead_outbox_events`.
- Bot DLQ: `eventbus_ingestdlq` — после устранения причины пометить `replayed_at` (replay через
  повторный POST с тем же `event_id` → дедуп защитит).
- Ingest 401 `no_secret` на всё → F0 (§3) не настроен.

## 9. Smoke-прогоны и drift-контроль

- **Smoke:** `scripts/pilot_smoke` — после каждого deploy, после каждого flip (§3), ежедневно
  в неделю 4. Отчёт `--md` складывать в `reports/` (вне репо) и ссылку — оркестратору.
- **Drift-контроль:** еженедельно (точки: 2026-07-26, 08-02, 08-09) — fetch + status всех
  5 репо по процедуре `docs/qa/PILOT_BASELINE_2026-07-19.md` §4 → отчёт оркестратору.

## 10. Открытые пункты (владельцы)

| # | Пункт | Владелец | Срок |
|---|---|---|---|
| O1 | F0 `EVENT_INGEST_HMAC_SECRET` — загрузка из env в bot settings | оркестратор/W3 | до первого smoke на staging |
| O2 | Имя выключателя concierge/memory-ask (§7) | W5 | до 12.08 |
| O3 | Юр. тексты (оферта, чеки) | юрист | 01.08 |
| O4 | KYC мастеров в ЮKassa | ops | 08.08 |
| O5 | D-3: состав `OUTBOX_EXTERNAL_DELIVERY_TOPICS` | оркестратор | staging-фаза |
