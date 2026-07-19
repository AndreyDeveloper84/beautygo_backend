---
node_id: ayla.ai-system.memory-lifecycle
title: Ayla Memory Lifecycle Specification
type: specification
status: draft
version: "0.1"
owner: Founder / Product Architecture
priority: P0
knowledge_area:
  - ai-system
domain:
  - cross-domain
concerns:
  - privacy
  - safety
  - governance
system_owner:
  - shared
source_repository: ayla-knowledge
created: 2026-07-19
updated: 2026-07-19
source_kind: draft
classification: internal
data_sensitivity: none
security_sensitivity: low
ai_indexing: allowed
export_policy: full
tags:
  - ayla
  - ayla/ai-system
  - ayla/memory
  - type/specification
depends_on:
  - "[[Ayla Constitution]]"
  - "[[Ayla Decision Log]]"
related:
  - "[[Ayla Knowledge Architecture Specification]]"
review_cycle: event-driven
---

# Ayla Memory Lifecycle Specification (v0.1 DRAFT)

> **ЧЕРНОВИК W6 (2026-07-19) → оркестратору; целевой дом — `ayla-knowledge/03 AI System/`.**
> Не нормативный до утверждения владельцем. Реализует: Constitution ст. V, VI, VII, IX, XIV;
> AYLA-DEC-0002 (память — центр); PILOT_CONTRACTS v1.3.0 (C5, AMD-010).
> Отражает фактическое состояние кода на 2026-07-19 (Ayla green-зона + internal API v1.0,
> bot MemoryEntry + consent cascade, ai-core `build_memory_block` v0.9.0).

## 1. Назначение и scope

Единые правила жизненного цикла фактов о пользователе: **откуда факт появляется, с какой
уверенностью используется, сколько живёт, как исправляется и удаляется.**

- **Пилот (2026-08-15):** green-зона (см. §3), источники 1–3 (§4), удаление по C5 (§7).
- **Post-pilot (вне scope, Contracts §9):** yellow/red-зоны с at-rest шифрованием и расширенным
  retention, обезличивание транзакционных данных, embeddings/RAG-память.

## 2. Термины

- **Факт** — атомарная единица памяти (поле/запись) с обязательными атрибутами: `source`,
  `zone`, `confidence`, `created_at`, `updated_at`, `consent_scope`, `expires_at|review_after`.
- **Declared** — прямо сообщённое пользователем. **Observed** — надёжно зафиксированное системой
  событие. **Inferred** — гипотеза Ayla из поведения (Constitution ст. V — не смешивать).
- **Зона** — класс чувствительности (§3). **Consent-scope** — цель обработки, к которой привязан факт.

## 3. Зоны чувствительности

| Зона | Состав (примеры) | Пилот | Правила |
|---|---|---|---|
| **green** | предпочтения услуг/мастеров, бюджет, район, временные окна, бытовые ограничения («кожа реагирует на крем» — не диагноз), избранное, история вопросов should_ask | ✅ единственная зона пилота | хранение открытым текстом; доступна рекомендациям при consent `memory_green`; export по C5 |
| **yellow** | пищевые паттерны с микро-флагами, сон/нагрузка, финансовые привычки | ❌ post-pilot | at-rest шифрование, отдельный consent, не влияет на ranking без явной цели, retention ≤ 90д без переподтверждения (проект из S3.2) |
| **red** | health-маркеры, психоэмоциональные состояния, всё safety-critical (аллергии с риском вреда) | ❌ post-pilot | red не возвращается в GET по умолчанию; отдельный access-log; строгий minimax доступа; запись — только declared/professional, НЕ inferred (ст. X запрет скрытой диагностики) |

**Правило границы:** если факт green-зоны начинает нести health-смысл («аллергия»), он обязан
переехать в red-зону при её появлении; в пилоте такие факты **не собираются** — should_ask
не задаёт health-вопросов (словарь тем — приложением к spec, TBD).

## 4. Источники фактов

| Source | Что | Реализация (2026-07-19) | Confidence при рождении |
|---|---|---|---|
| **S1 declared via DM** | ответы на should_ask-вопросы консьержа | Ayla `personal-context` internal API (should_ask/mark-asked/skip) + bot MemoryEntry `source='explicit'` | 1.0 (declared) |
| **S2 observed/behavioral** | повторные записи, избранное, паттерны (`infer_user_patterns`) | Ayla Celery (verify beat) + bot `source='inferred'` | 0.4–0.7 (гипотеза) |
| **S3 chat extraction** | structured-факты из диалога | Ayla hint-read (WRITE — S3.4, частично) + bot `record_explicit_green_facts` | declared: 1.0; auto-extract без подтверждения: ≤0.7 |
| **S4 professional** | записи мастера (safety-наблюдения) | post-pilot (Constitution ст. III: автор + время + «наблюдаемый факт») | n/a в пилоте |
| **S5 device** | носимые устройства | post-pilot | n/a в пилоте |

Инварианты (ст. V, VII): inferred никогда не перезаписывает declared; поведение не отменяет
прямые слова без достаточных подтверждений; молчание/отмена ≠ согласие или отказ от цели.

## 5. Confidence и использование

- **Шкала:** declared = 1.0 · inferred стартует 0.4–0.7 · повышается повторными независимыми
  наблюдениями (+Δ за подтверждение, −Δ за противоречие; точные Δ — [TBD владельцу модели]).
- **Тиеры рендеринга (ai-core v0.9.0, зафиксировано):** ≥0.8 — подаётся как факт;
  0.4–0.8 — вероятностный язык («возможно», ст. VII); <0.4 — не факт, а кандидат в
  уточняющий вопрос (clarify) либо не используется.
- **Явная обратная связь** (исправление/вето пользователя) мгновенно ставит confidence 1.0
  (исправленное значение) или 0 + `disabled` (вето) — и имеет приоритет над любой гипотезой.
- **Safety-critical** (когда red-зона появится): confidence не влияет на сохранность — ст. IX.

## 6. TTL, актуальность, переподтверждение

Проект значений (DRAFT — утверждает владелец):

| Класс факта | Пример | `review_after` | Просрочено → |
|---|---|---|---|
| Устойчивые предпочтения | любимый мастер, район | 180д | мягкий downgrade confidence (−0.1/период), остаётся green |
| Временные состояния | «кожа реагирует на крем», «готовлюсь к событию» | 30д | статус `needs_reconfirm`; не используется как факт (tier <0.4) |
| Поведенческие паттерны | любимое время записи | 90д | пересчёт из свежих наблюдений или archive |
| Вето/отказы | «не предлагать маникюр» | без TTL | действует до отмены пользователем (ст. VII) |
| Safety-critical (post-pilot) | аллергия | не исчезает по давности (ст. IX) | только `needs_reconfirm`, продолжает ограничивать |

- `needs_reconfirm` — повод для should_ask (если проходит rate-limit §8), не для молчаливого
  продолжения использования.
- Archive ≠ использование: архивный факт не попадает в memory-блок и ranking.

## 7. Исправление, удаление, 152-ФЗ

- **Просмотр/исправление:** экран «Мои данные» (W4): каждый факт — с источником и датой (ст. VII, IX).
- **Удаление факта:** прекращает использование немедленно (не влияет на рекомендации со
  следующего запроса); при ручном удалении — объяснение последствий (ст. IX).
- **Полное удаление (C5):** `DELETE /api/v1/customer/me/personal-data/` → каскад: Ayla
  personal-context wipe (AMD-006, идемпотентно) + bot MemoryEntry soft-delete + forget-all
  tombstone + consent withdraw cascade. Повтор — 200 (идемпотентность).
- **Аудит удаления (AMD-010):** `AnalyticsEvent personal_data_deleted` (actor, timestamp, scope)
  **без удалённых значений**.
- **Retention вне контракта пилота:** транзакционные записи (записи, платежи) хранятся по
  закону; обезличивание — post-pilot. Пользователю различие объясняется (ст. IX: активная
  модель / операционные данные / аудит / обезличенная аналитика).
- **Export (C5.1):** синхронный JSON (профиль subset + полный green-контекст + consents-история).

## 8. Проактивность и rate-limit (ст. VI, X)

- should_ask задаётся только если ответ нельзя получить иначе и есть понятная цель.
- Лимиты (DRAFT): ≤ [TBD, проект 2] вопросов/неделю; cooldown темы после `skip` — [TBD, проект 30д];
  мягкий отказ — один раз выбор «позже/не спрашивать»; жёсткий отказ — тема закрывается (ст. X).
- Настройки пользователя (каналы/частота/темы) имеют приоритет над дефолтами.

## 9. Согласия (ст. XIV)

- **Раздельные scope:** `personal_data` (хранение/обработка), `memory_green` (использование
  green-памяти в персонализации). Отзыв независим; каскад withdraw реализован (bot, 2026-07-19).
- Отказ от персонализации **не блокирует** ручной поиск/просмотр/запись (ст. XIV).
- Один consent ≠ универсальное разрешение; новая цель = новое основание.

## 10. Аудит и трассируемость (ст. VII)

- События (есть): `question_shown/answered/skipped`, `context_used_in_recommendation`,
  `personal_data_deleted`. Существенная рекомендация — восстановимый след (правила, факты,
  источники) без избыточного сырого текста.
- Каждая запись spec ↔ статьи Constitution: V (§4), VI (§8), VII (§5,§7,§10), IX (§6,§7),
  XIV (§9), IV (composer — экономическая нейтральность, в Killer PRD §6.4).

## 11. Open Questions (на владельца/оркестратора)

1. Утвердить TTL-таблицу §6 и Δ-confidence §5 (значения — проекты W6).
2. Словарь запрещённых тем should_ask пилота (health-граница §3) — кто пишет (product+legal)?
3. Судьба двух consent-базисов (PERSONAL_DATA vs memory_green — зафиксировано W3 2026-07-18 как deliberate): оставить два или свести — нужна запись Decision Log.
4. Red-зона: подтвердить post-pilot (S3.2) или выделить отдельный track с security.
5. Владелец spec после утверждения (product architecture?) + дата перевода draft→approved.
