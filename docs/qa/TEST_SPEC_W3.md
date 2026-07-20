# Тест-спецификация W3 — Bot Backend

**Поток:** W3 (ai-bot-platform; booking proxy, eventbus, memory, privacy, master_api proxies) · **Версия:** 2026-07-19, W6.
**Основание:** PILOT_CONTRACTS v1.3.0 (C1 клиент, C2/C3 consumer, C4 consumer, C5.1/6.2 bot, R1 доставка, AMD-001/002/005/007/008), acceptance §10 сценарии 1, 3, 6, 7.
**Правила:** реализацию пишет W3; W6 верифицирует. Команда прогона: `uv run pytest -m "not smoke"`.
**Маркировка:** ✅ существует (в `dev` @ `6ff8d17`) · ➕ должен быть добавлен · 🔶 зависит от решения оркестратора.

## W3-AC-01. Booking через Ayla REST (§10.1, AMD-002)
- **Сценарий:** бот ведёт create/cancel/reschedule через Ayla internal REST под `AYLA_INTERNAL_API_TOKEN`.
- **Ожидаемое:** Bearer + `X-External-User-ID` + `X-Idempotency-Key` на writes; gate `BOOKING_VIA_AYLA_REST`; mirror `RemoteBookingProxy` консистентен (native move сохраняет id).
- **Тесты:** ✅ `test_ayla_write_lifecycle.py`, `test_provider_ayla.py`, `test_remote_proxy_event_id.py`, route-table `test_contract_route_table.py`.
- **➕ Пробел G-1 → закрыт (2026-07-19, по отчёту W3, dev `8817190`):** `payment_required` на create добавлен. Осталось для приёмки: route-table расширить проверкой тела create; e2e `payment_required=false` → CONFIRMED — smoke S1 (волна 3).

## W3-AC-02. Приватность долга на клиенте (§2, §10.8)
- **Ожидаемое:** 409 `SUBSCRIPTION_PAST_DUE` от Ayla → клиенту нейтральное «Сейчас запись к этому специалисту недоступна» + предложение другого мастера/времени; причина долга не утекает в тексты/логи канала.
- **Тесты:** ✅ mapping в `provider.py` (`YClientsSpecialistUnavailableError`); ➕ `test_past_due_neutral_message` — assert текста (нет «долг/оплат/подписк») + наличие альтернативы.

## W3-AC-03. Catalog link + покрытие (AMD-001, S1-B)
- **Ожидаемое:** матчинг по (`category_slug`, нормализованное `name`) + duration tiebreaker; нормализация: lower, trim, ё→е, схлопывание пробелов, удаление «»; mapping file исключений; отчёт `matched auto/manual/unmatched`; порог ~100% (Пенза) → флип гейта.
- **Тесты:** ✅ `test_linking.py` (casefold/ё→е/схлопывание, эвикт ambiguous, `--fail-under`).
- **Отклонения → закрыты (2026-07-19, по отчёту W3, dev `8817190`):** link доведён до AMD-001 (duration tiebreaker, «»-stripping, mapping file). Осталось: прогон на каталоге Пензы на staging + coverage-отчёт оркестратору (go/no-go флипа, `8817190` в dev).

## W3-AC-04. Eventbus ingest (§5, AMD-007/008)
- **Ожидаемое:** конверт 10 полей; дедуп по `event_id` (повтор → handled once); unknown `event_version` → 422 + DLQ; unknown name → **400 `invalid_event_name`** (envelope-allowlist отклоняет до dispatch; путь 422+DLQ для unknown name достижим только при дрейфе двух allowlist'ов — сверено с кодом 2026-07-19, `ingest_envelope.py`/`views.py`); billing-топики в ALLOWED.
- **Тесты:** ✅ `test_ingest_view.py`, `test_e2e_ingest_smoke.py`, `test_allowlist_sync.py`, `test_contract_fixtures.py`, `test_s4_vocab_contract_regression.py`.
- **⚠️ Staging preflight:** `EVENT_INGEST_HMAC_SECRET` не загружается из env в settings (config-gap) — без фикса ingest отвечает 401 `no_secret` на всё (runbook §3, F0).

## W3-AC-05. Billing consumers → уведомления мастеру (§5 consumer) — после G-2
- **Ожидаемое:** `subscription.past_due` → MAX-уведомление мастеру (сумма долга, CTA погасить); `subscription.activated` → подтверждение; `billing.fee_charged` → уведомление о начислении; резолв `specialist_id` (User UUID) → CatalogMaster; tenant-авторизация.
- **Тесты:** ✅ консьюмеры с валидацией payload (log-only сейчас); ➕ `test_billing_notifications.py` — после закрытия specialist mapping (K-2/G-2): доставка правильному мастеру, дедуп по event_id, текст без лишних данных.

## W3-AC-06. R1 напоминания (§7)
- **Ожидаемое:** `booking.created` → планирование T−24h; повторная доставка события — не дублирует (unique `(appointment_id, kind)`); cancel/reschedule → отмена/перепланирование идемпотентно; cross-tenant изоляция; dispatch loop отправляет MAX-сообщение в окне.
- **Тесты:** ✅ `test_booking_consumer.py` (4); ➕ проверить/добавить тест dispatch-задачи (`apps/bookings/tasks.py`): due-напоминание реально уходит в канал (mock MAX), повторный тик не шлёт дважды.
- **🔶 D-2 → AMD-012 (решено 2026-07-19):** T−2h принят как допустимое второе напоминание; дедуп отдельно по каждому offset; smoke S6 проверяет оба.

## W3-AC-07. Memory + consent (§10.7)
- **Ожидаемое:** inferred-память пишется только при consent (memory_green / PERSONAL_DATA), дедуп, forget-all tombstone; вызовы `should_ask/mark-asked/skip` гейтятся (`BLOCKED_CONSENT` short-circuit); withdraw каскадом отзывает оба базиса.
- **Тесты:** ✅ `test_memory_inferred.py`, `test_memory_consent.py`, `test_privacy.py` (частично).

## W3-AC-08. C5 bot endpoints (§6, §10.6)
- **Ожидаемое:** `GET /api/v1/customer/me/personal-data/export/` — один JSON (Ayla export verbatim + MemoryEntry + ConsentRecord), `Content-Disposition: attachment`; `DELETE …/personal-data/` — каскад (upstream Ayla + memory soft-delete + consent withdraw); идемпотентность (upstream 404 = deleted); частичный сбой → 502; аудит без значений; auth обязателен.
- **Тесты:** ✅ `test_privacy.py` (9).

## W3-AC-09. C2/C3 proxies в master_api (§3/§4 consumer)
- **Ожидаемое:** verbatim-прокидывание `data`; 404/502 маппятся; Bearer master-авторизация; до закрытия G-2 — fail-closed 503 `specialist_mapping_unavailable`.
- **Тесты:** ✅ `apps/master_api/tests/test_billing.py` (6); ➕ после K-2: интеграционный `test_proxy_end_to_end` против staging Ayla (волна 3, smoke W6).

## W3-AC-10. Outbox Ayla → бот: round-trip (S-4, волна 3)
- **Сценарий:** событие booking.*/billing.* эмитировано в Ayla → доставлено в ingest бота → обработано.
- **Ожидаемое:** после 🔶 D-3 (включение `OUTBOX_EXTERNAL_DELIVERY_TOPICS`): e2e round-trip < N сек; дедуп при повторной доставке; HMAC/подпись по ADR-0009.
- **Тесты:** ➕ smoke `scripts/pilot_smoke/eventbus_roundtrip` (W6, волна 3) + контрактный фикстурный тест (есть ✅ `test_contract_fixtures.py`).
