# Тест-спецификация W5 — AI-core → Concierge

**Поток:** W5 (ayla-ai-core + concierge wiring в ai-bot-platform/djangoproject) · **Версия:** 2026-07-19, W6.
**Основание:** бриф W5 (релиз памяти, wiring консьержа, should_ask end-to-end, anti-hallucination), acceptance §10 сценарий 7; Ayla Constitution ст. V–VII, IX (источники знаний, объяснение, жизненный цикл); RELEASING.md ayla-ai-core.
**Правила:** реализацию пишет W5; W6 верифицирует. Команды: `pytest` (ayla-ai-core), `uv run pytest -m "not smoke"` (bot).
**Маркировка:** ✅ существует · ➕ должен быть добавлен · 🔶 зависит от решения/другого потока.

## W5-AC-01. Релиз ai-core v0.9.0 (бриф W5.1)
- **Ожидаемое:** `build_memory_block` экспортирован из `__init__.py`; зелёная зона; confidence-тиers (≥0.8 факт, 0.4–0.8 смягчение, <0.4 → clarify); cap `max_facts`; запрет неизвестных ключей; snapshot публичного API; CHANGELOG; тег.
- **Тесты:** ✅ `tests/test_memory.py` (9) + `test_public_api_surface.py`; релиз v0.9.0 подтверждён (tag, CHANGELOG 2026-07-18). **Acceptance-верификация W6: пройдено.**
- **Замечание:** literal-output snapshot рендеринга блока отсутствует (только API-surface) — ➕ опционально, не блокер.

## W5-AC-02. Парный бамп SHA в потребителях (бриф W5.1, RELEASING §4)
- **Ожидаемое:** оба backend пинуют **коммит-SHA** `f773e7d` (v0.9.0; формат — 40-char SHA, не тег); smoke-import тест обновлён.
- **Статус:** ai-bot-platform — ✅ на `pilot/bot-backend`; djangoproject — ❌ (`requirements.txt:93` пин `e73a1b4` = v0.8.1).
- **Тесты:** ➕ в djangoproject: обновить пин + `tests/smoke/test_ayla_import.py` (по RELEASING §4); ➕ CI-проверка «SHA пина == SHA тега v0.9.0» (защита от рассинхрона).

## W5-AC-03. Concierge wiring: диалог → подбор → бронь (бриф W5.2, DRF-241) — после W1/W3
- **Сценарий:** пользователь в DM просит услугу → консьерж подбирает из реального каталога → создаёт запись через Ayla REST.
- **Ожидаемое:** booking tools консьержа ходят через RemoteBookingProxy (не stub); выбор слота подтверждается пользователем; ошибки API — дружелюбный fallback.
- **Тесты:** ➕ интеграционный `test_concierge_booking_flow.py` (mock Ayla REST: подбор → create → CONFIRMED); e2e staging — волна 3 (smoke W6). 🔶 зависит от G-1 (`payment_required`).

## W5-AC-04. Memory-ask end-to-end (§10.7, бриф W5.3) — критический путь пилота
- **Сценарий:** консьерж вызывает `should_ask` → органично задаёт вопрос в DM → ответ пользователя сохранён → следующая рекомендация учла ответ.
- **Ожидаемое:** вызов только при eligibility и consent `memory_green` (иначе — молча пропуск); инъекция memory-блока в промпт (ai-core `build_memory_block`); вопрос не повторяется (`mark-asked`); `skip` при отказе; сохранение ответа в Ayla personal-context (declared) и/или bot MemoryEntry — по источнику.
- **Статус:** bot-гейты и клиент ✅ (W3); **wiring в консьерже ❌** (`pilot/concierge` @ `f5a1fd0` отстаёт от dev, нет `personal_context_client`; сейчас только `record_explicit_green_facts` + grounding).
- **Тесты:** ➕ `test_concierge_should_ask.py`: eligibility=yes + consent → вопрос задан ровно один раз; no-consent → вопрос НЕ задан и ничего не сохранено; ответ → контекст обновлён; ➕ регрессия «рекомендация учла память» (prompt содержит memory-блок с новым фактом); e2e сценарий §10.7 — волна 3 (smoke W6 memory-ask).

## W5-AC-05. Anti-hallucination на реальном каталоге (бриф W5.4)
- **Ожидаемое:** консьерж не выдумывает услуги/мастеров/цены вне каталога; при отсутствии данных — честное «не нашёл» (Constitution ст. II, XII).
- **Тесты:** ➕ eval-набор (фиксированные промпты против реального каталога Пензы): assert, что каждая named-сущность в ответе существует в каталоге; прогон в CI nightly или pre-release gate. Формат и порог — W5 предлагает, W6 включает в runbook-чеклист.

## W5-AC-06. Consent-гейт в MAX DM handler (частично есть)
- **Ожидаемое:** `can_store_green_memory` перед любой записью; explicit facts пишутся только с consent; grounding не протекает вне гейта.
- **Тесты:** ✅ wiring в `apps/channels/max/handler.py` (на `pilot/concierge`); ➕ регрессионный тест гейта на handler-уровне (consent отозван → запись не происходит, ответ пользователю корректный).
