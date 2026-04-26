# Food Scanner — Vendor & Architecture Decision

> Status: DECIDED · Date: 2026-04-26 · Approver: Andrey
> Plan: **Plan Y+ — Multi-vendor router in M5 + self-host roadmap to Phase 6**
> Pilot target: M5 Penza, 2026-07-15

## Decision

**Ship food scanner в M5 пилот через multi-vendor router** с двумя
backend-ами: OpenAI Vision (primary) + YandexGPT Vision (fallback).
Параллельно — заложить интерфейс для third backend (self-host ViT) с
миграцией в Phase 6.

## Context

| Constraint | Value |
|---|---|
| Pilot deadline | 2026-07-15 (~11 weeks из 2026-04-26) |
| Food scanner в M5 | Обязательно |
| Vendor lock-in tolerance | Низкая (multi-vendor required) |
| 152-ФЗ posture | Photo на наш сервер ИЛИ EU OK; PII не смешиваем с фото |
| In-house ML aspiration | Да, к Phase 6 |
| MLE on team | Нет |

## Architecture

```
                ┌─────────────────────────────────┐
   POST /api/v1/nutrition/scan
                │
                ▼
       FoodScannerRouter
                │
   ┌────────────┼────────────┬─────────────┐
   │            │            │             │
   ▼            ▼            ▼             ▼
OpenAI      Yandex       Self-host    (manual entry
gpt-4o      Vision       ViT model     fallback —
Vision      Pro                        pilot edge)
(primary)   (fallback)   (Phase 6)
```

### Router rules (MVP, Phase 5 → M5)

```python
# nutrition/services/food_scanner_router.py (planned)
def scan(image_bytes, user) -> ScanResult:
    primary = _provider_for(user, "primary")     # default: openai
    try:
        return primary.scan(image_bytes, user)
    except (ProviderTimeout, ProviderUnavailable, LowConfidence) as exc:
        logger.warning("food_scanner.fallback primary=%s err=%s", primary, exc)
        fallback = _provider_for(user, "fallback")  # default: yandex
        return fallback.scan(image_bytes, user)
```

`_provider_for` reads `FOOD_SCANNER_PRIMARY` / `FOOD_SCANNER_FALLBACK` env
vars. Default `openai` / `yandex`. Per-user override via
`UserProfile.food_scanner_provider_pref` is a Phase 6 nice-to-have.

### Data we store
- `FoodScan` — uuid, user_id, image_url (S3), recognized_dish, confidence,
  nutrition JSON, **provider used** (`openai`/`yandex`/`viT`),
  `provider_request_id`, latency_ms, created_at.
- We do NOT store the photo bytes inline — S3 only, 30-day TTL.

## Provider details

### OpenAI Vision (primary)

- Model: `gpt-4o` (multimodal). Pricing ~$0.005-0.01 per image input + ~$0.015/1K output.
- Calls go through `OPENAI_PROXY` (px6) — same plumbing as AI Chat.
- Prompt: structured "извлеки название блюда, оцени порцию в граммах,
  верни JSON {dish, portion_g, ingredients[]}". Nutrition lookup —
  второй шаг через Open Food Facts / USDA.
- 152-ФЗ note: фото уходит на сервер OpenAI (US). Согласие пользователя
  на обработку обязательно (см. consent screen в pilot UX).

### YandexGPT Vision Pro (fallback)

- Model: YandexGPT 5 Pro Vision (через Yandex Cloud).
- Endpoint: `https://llm.api.cloud.yandex.net/...` — серверы РФ.
- Pricing: ~5-10 руб/запрос на пилотных объёмах.
- Triggers fallback при: OpenAI 5xx, timeout > 5s, OR confidence < 0.4.
- 152-ФЗ-friendly: РФ-юрисдикция, без proxy.

### Self-host ViT (Phase 6 plug-in)

- Базовая модель: HuggingFace `nateraw/food-101-vit` (или аналог с
  лучшим RU coverage когда выберем).
- Сервер: cloud GPU ($80-200/мес).
- Wrapper: FastAPI или Django micro-service за тем же routerom.
- **NOT в M5 scope.** Включается в Phase 6 после пилота, когда у нас
  будут реальные данные о cost / quality / volume.

## Why this and not the alternatives

### Why not "OpenAI only"
- Single vendor risk на critical pilot path
- 152-ФЗ — спорная trajectory, лучше иметь РФ-fallback готовый

### Why not "Yandex only"
- Меньше зрелости в multimodal (vs gpt-4o)
- Уже есть proxy-инфраструктура для OpenAI

### Why not "self-host ViT в M5"
- 50-65% accuracy на RU блюдах = плохой UX в пилот
- Мы не AI-компания, нет MLE для итераций
- Для in-house надо собирать RU dataset (3-6 месяцев)

### Why not "defer food scanner из M5"
- User explicitly said: обязательно в M5 (#2)

## Water tracker — отдельно

**Решено:** manual tap-button entry в MVP. Никакого ML.

UX: 4 кнопки (`100мл / 250мл (стакан) / 500мл (бутылка) / своё`) +
прогресс-бар к цели (2L по умолчанию).

ML water detection — Phase 7+ если когда-либо появится data signal.

## Implementation slices

### Slice 1: Vendor SDKs (1-2 дня)
- [ ] Add `yandexgpt-sdk` или прямые HTTP вызовы в `nutrition/providers/yandex.py`
- [ ] Add `nutrition/providers/openai_vision.py` (использует
      существующий `ai.services.llm_client.get_openai_client`)
- [ ] `nutrition/providers/base.py` — abstract `FoodScannerProvider`
      с методом `scan(image_bytes, user) -> ScanResult`

### Slice 2: Router + endpoint (2 дня)
- [ ] `nutrition/services/food_scanner_router.py`
- [ ] `POST /api/v1/nutrition/scan` endpoint per spec v2.0 §FOOD SCANNER
- [ ] `FoodScan` model + migration
- [ ] Settings: `FOOD_SCANNER_PRIMARY=openai`, `FOOD_SCANNER_FALLBACK=yandex`,
      `YANDEX_VISION_API_KEY`, `YANDEX_VISION_FOLDER_ID`

### Slice 3: Nutrition lookup (1 неделя)
- [ ] Open Food Facts / USDA database integration
- [ ] Manual RU блюда additions (борщ, оливье, греча, ...) — ~50 ручных entries
- [ ] `Service /api/v1/nutrition/food-log` per spec
- [ ] `Service /api/v1/nutrition/summary` per spec

### Slice 4: Water tracker (2 дня)
- [ ] `WaterLog` model + migration
- [ ] `POST /api/v1/nutrition/water` + `DELETE /api/v1/nutrition/water/{id}`
- [ ] `GET /api/v1/nutrition/water/today`
- [ ] Все per spec v2.0 §FOOD SCANNER+NUTRITION

### Slice 5: Consent + analytics (1 день)
- [ ] Consent screen на mobile (фото уходит на наш сервер / EU / RU
      в зависимости от vendor)
- [ ] Analytics event `food_scan` с полем `provider` для пост-пилот
      A/B анализа

**Total estimate:** ~2.5-3 недели CC (подходит к pilot timeline ремонтом
mobile UI Phase 3).

## Phase 6 follow-up

### Self-host ViT enablement (Q3-Q4 2026)
- [ ] Spike: prototype `nateraw/food-101-vit` на dev VPS
- [ ] Benchmark на 30 RU блюдах (collected from pilot scans)
- [ ] Decision gate: качество ≥ 60% top-1 → ship as third provider
- [ ] If ≥ 75% — switch as primary, vendors → fallback only

### Fine-tune под RU кухню (only if data signals it)
- [ ] Если pilot retention положительный — собираем RU dataset
- [ ] Толока labeling: 100 блюд × 100 фото = $1-3k
- [ ] Fine-tune over 1-2 недели
- [ ] Re-benchmark, deploy

### Vendor cost dashboard
- [ ] Track $ spent per vendor per day in Sentry / Grafana
- [ ] Alert если daily total > $50 в пилот

## Open follow-ups

- [ ] Yandex Vision API key procurement (нужен у Andrey)
- [ ] Consent text UX — короче чем "Ваше фото уйдёт на сервер OpenAI..."
      (review с PM)
- [ ] Per-user vendor preference storage (Phase 6)
- [ ] Photo TTL policy на S3 (30 days в plan, нужно подтверждение Privacy)
- [ ] Cost attribution: кто платит за vendor calls anonymous user-ов?
      (см. AI Chat anonymous limit pattern — same logic применим)

## References

- `docs/AI_CHAT_PLAN.md` — параллельный поток (AI Chat использует ту же
  OpenAI proxy инфраструктуру)
- Notion API Spec v2.0 §FOOD SCANNER + §NUTRITION
- `project_ai_foundation.md` memory — vendor decision context
- `project_m4_scope_reduction.md` memory — token #2 = "Food recognition API"

---

*Last updated: 2026-04-26*
