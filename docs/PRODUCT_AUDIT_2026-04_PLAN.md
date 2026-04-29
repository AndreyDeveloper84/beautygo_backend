# Product Audit BeautyGO — План (2026-04-27)

> PM-аудит проекта с использованием pm-skills marketplace (Teresa Torres / Marty Cagan / JTBD / OST / RICE).
> Финальный артефакт: `docs/PRODUCT_AUDIT_2026-04.md`

## Цель аудита

Получить независимый продуктовый взгляд на BeautyGO: что построено правильно, где есть слепые зоны, какие гипотезы не проверены, что приоритезировать перед M4-pilot (2026-06-30).

## Вход

- `CLAUDE.md` — статус реализации, архитектура, бизнес-правила
- Memory: roadmap (Phase 2 merged, AI Chat не начат, Food Scanner Slice 1 готов)
- `docs/AI_CHAT_PLAN.md`, `docs/FOOD_SCANNER_DECISION.md`, `docs/PRD_Ayla_Killer_Scenario_v1.0.md`
- Notion API spec v2.0 + PRD (через MCP при необходимости)
- Внешний контекст: рынок RU-бьюти-сервисов (Yclients, DIKIDI, Altegio, Booksy)

## Структура аудита (10 секций)

| # | Секция | Skill | Что даёт |
|---|--------|-------|----------|
| 1 | **Product Strategy Canvas** | `product-strategy` (9 секций: Vision → Defensibility) | Целостная стратегия Two Apps |
| 2 | **Value Proposition (JTBD)** | `value-proposition` (6-part) × 2 (client + specialist) | Что нанимает клиент/мастер |
| 3 | **Personas** | `user-personas` × 2 | Кто целевые клиент и мастер |
| 4 | **Customer Journey Map** | `customer-journey-map` × 2 | Где трение в воронке |
| 5 | **Market Sizing** | `market-sizing` (TAM/SAM/SOM, RU) | Размер возможности |
| 6 | **Competitive Analysis** | `competitor-analysis` (Yclients, DIKIDI, Altegio, Booksy) | Где наша дифференциация |
| 7 | **Assumption Mapping** для AI Chat | `identify-assumptions-existing` + `prioritize-assumptions` | Что проверить до запуска AI Chat |
| 8 | **Pricing Strategy** | `pricing-strategy` (валидация 8% комиссии) | Бизнес-модель устойчива? |
| 9 | **Pre-mortem M4-pilot** | `pre-mortem` (Tigers/Paper Tigers/Elephants) | Что убьёт пилот |
| 10 | **Prioritized Roadmap** | `outcome-roadmap` + `prioritization-frameworks` (RICE) | Что делать в следующие 2 спринта |

## Фазы выполнения

**Phase 1 — Foundation** (секции 1–3): стратегия, JTBD, персоны
**Phase 2 — Market** (секции 4–6): journey, sizing, конкуренты
**Phase 3 — Validation** (секции 7–9): assumptions AI Chat, pricing, pre-mortem
**Phase 4 — Synthesis** (секция 10): приоритезированный roadmap + ключевые рекомендации

После каждой фазы — checkpoint (показываю результат, корректируем).

## Финальный артефакт

`docs/PRODUCT_AUDIT_2026-04.md` — единый документ со всеми 10 секциями + executive summary в начале (что нужно сделать в первую очередь, какие риски критичны, какие гипотезы протестировать).

## Открытые вопросы (нужно подтверждение)

1. **Scope**: все 10 секций или сократить до топ-5 (стратегия, персоны, конкуренты, AI Chat assumptions, roadmap)?
2. **Глубина**: глубокий разбор каждой секции (~300–500 слов) или более сжатые выжимки (~150 слов)?
3. **Источники**: ограничиться `CLAUDE.md` + memory или подтянуть Notion PRD/spec через MCP?
4. **Рынок**: использовать публичные данные (Yclients ~250к мастеров, etc.) или просить вас подтвердить цифры?
5. **Output language**: документ на русском (как и общение), термины фреймворков на английском в скобках — ОК?
