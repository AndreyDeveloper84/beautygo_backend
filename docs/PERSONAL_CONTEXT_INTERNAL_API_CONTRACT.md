# Personal-Context Internal API — FROZEN CONTRACT (declared prefs)

> **v1.0 · FROZEN · 2026-07-09**
> Owner: Ayla (`beautygo_backend`). Consumer: `ai-bot-platform` concierge (**M-B1**).
> Source of truth in code: `users/internal_personal_context_api.py`,
> `users/internal_users_urls.py`, commit `63af097e` (A1a).
>
> **Scope after founder pivot (2026-07-09):** пилотная память живёт в **боте**
> (inferred zones/Fernet/red-log per ADR-0011/0006). Ayla хранит **только
> declared prefs** — зелёная зона, открытые, без шифрования/зон/red-log. Этот
> API отдаёт боту *declared prefs* как **дополнение** к его собственной
> inferred-памяти. Зоны 🟡/🔴 на стороне Ayla **не строятся** — их здесь нет и
> не будет по этому контракту.

## Договор совместимости

Контракт **заморожен**. Bot M-B1 может кодировать пути/тело/auth/поля как есть.
Изменения — только **аддитивные** и только через bump версии этого документа +
уведомление оркестратора:

- Новое поле в каталоге declared prefs → additive, bot игнорирует незнакомые ключи.
- Новый endpoint → additive.
- **Запрещено без нового контракта:** переименование/удаление поля, смена
  типа поля, смена envelope, смена auth, смена путей.

Bot-клиент ДОЛЖЕН быть толерантен к неизвестным ключам в `context` (forward-compat).

---

## Аутентификация

Все endpoints — **service-to-service**, без клиентского JWT.

```
Authorization: Bearer <AYLA_INTERNAL_API_TOKEN>
```

- Permission: `users.permissions.IsInternalBearer` (constant-time сравнение).
- `authentication_classes = []` → `request.user` остаётся Anonymous; резолв
  человека — по `ayla_user_id` из пути.
- Пустой `AYLA_INTERNAL_API_TOKEN` в настройках → **fail closed** (все запросы 403).
- Неверный/отсутствующий Bearer → `401` или `403` (DRF permission denial).

**Consent-гейт (memory_green) enforce'ится на боте ДО вызова** (см. MEMORY_CONSENT_SPEC).
Ayla доверяет internal-Bearer вызову; серверный backstop — plug-in (A1b).

## Базовый префикс

```
/api/v1/internal/users/{ayla_user_id}/personal-context/...
```

`ayla_user_id` — UUID Ayla `User.id`. Невалидный UUID или несуществующий
пользователь → `404 USER_NOT_FOUND`.

## Envelope

Успех: `{ "data": <payload>, "meta"?: {...} }`
Ошибка: `{ "error": { "code": "<CODE>", "message": "<текст>", "details"?: {...} } }`

Коды ошибок (this contract): `USER_NOT_FOUND` (404), `VALIDATION_ERROR` (400),
`TOO_MANY_FIELDS` (400).

---

## 1. `GET /personal-context/` — читать declared prefs

Lazy-create строки контекста при первом доступе (пустой юзер = валидное пустое
состояние, **не** 404). Отдаёт весь каталог declared-полей.

**200:**
```json
{
  "data": {
    "ayla_user_id": "<uuid>",
    "context": {
      "preferred_districts": [],
      "preferred_time_slots": [],
      "price_range_min": null,
      "price_range_max": null,
      "diet_type": "",
      "skin_sensitivities": [],
      "prefers_flexible_cancellation": false,
      "workplace_district": "",
      "home_district": "",
      "favorite_masters": [],
      "min_rating_preference": null,
      "busy_days": []
    },
    "meta": { "filled_fields": 0, "updated_at": "<iso8601>" }
  }
}
```

`meta.filled_fields` — счётчик truthy-полей (осмысленно заполнено; `false/0/[]/""`
= дефолт, не считается).

**404:** `{ "error": { "code": "USER_NOT_FOUND", "message": "User not found." } }`

## 2. `PATCH /personal-context/` — писать declared prefs (идемпотентно)

Батч обновлений. **Идемпотентность:** last-write-wins per поле — повтор того же
тела даёт то же состояние. Пропущенные поля НЕ очищаются.

**Тело:**
```json
{
  "updates": [
    { "field": "preferred_time_slots", "value": ["evening"], "source": "explicit", "confidence": 1.0 },
    { "field": "price_range_max", "value": "2000.00" }
  ]
}
```

- `updates` — непустой список, **≤ 10** элементов (иначе `TOO_MANY_FIELDS`).
- `field` — ∈ каталог declared prefs (см. §5). Неизвестное поле → `400`.
- `value` — произвольный JSON, тип по каталогу поля.
- `source` — ∈ `{explicit, behavioral, conversational, transactional}`, default `explicit`.
  Пишется в `data_sources[field]` (provenance).
- `confidence` — `0.0..1.0`, default `1.0`, optional. **Принимается, но в A1a НЕ
  персистится** (per-field confidence — plug-in A1b / MemoryFact). Bot может слать,
  но не должен полагаться на возврат.

**200:** `{ "data": { "ayla_user_id": "<uuid>", "context": { ...полный каталог... } } }`

**400:** `VALIDATION_ERROR` (пустой/не-список `updates`, неизвестное поле),
`TOO_MANY_FIELDS` (> 10). **404:** `USER_NOT_FOUND`.

## 3. `GET /personal-context/ask-eligibility/` — какое ОДНО поле спросить

Обёртка над 8 anti-spam правилами `personalization_engine` (cooldown 24ч,
skip×2→пауза, not-first-interaction, …). Возвращает первого допустимого кандидата
в приоритетном порядке.

Приоритет кандидатов (fixed): `preferred_time_slots` → `price_range_max` →
`favorite_masters` → `workplace_district` → `home_district` → `busy_days` →
`min_rating_preference` → `diet_type`.

**200 (можно спросить):**
```json
{ "data": {
  "should_ask": true, "field": "preferred_time_slots", "reason_code": null,
  "explain": "<почему разрешено>", "prompt_hint": "Тебе удобнее записываться утром, днём или вечером?"
} }
```

**200 (нельзя):** `{ "data": { "should_ask": false, "blocked_by": "first_interaction" } }`
(`blocked_by` ∈ причины движка: `first_interaction`, `cooldown`, `skipped_twice`,
`no_candidate`, …).

## 4. `POST /personal-context/mark-asked/` — отметить, что вопрос ЗАДАН

Ставит 24ч cooldown. Тело: `{ "field": "<из ask-кандидатов>" }`.
**200:** `{ "data": { "ok": true } }`. Неизвестное поле → `400`.

## 5. `POST /personal-context/skip/` — пользователь пропустил вопрос

Инкремент skip-счётчика (skip×2 → пауза в движке). Тело: `{ "field": "<из ask-кандидатов>" }`.
**200:** `{ "data": { "ok": true, "skip_count": 1 } }`.

---

## Каталог declared-полей (green, открытые)

| Поле | Тип (JSON) | Прим. |
|------|-----------|-------|
| `preferred_districts` | `list[str]` | Районы/метро, где удобно записываться |
| `preferred_time_slots` | `list[str]` | ∈ `early_morning, morning, afternoon, evening, late_evening` |
| `price_range_min` | `str(decimal)` \| `null` | Рубли, 2 знака |
| `price_range_max` | `str(decimal)` \| `null` | Рубли, 2 знака |
| `diet_type` | `str` | ∈ `omnivore, vegetarian, vegan, keto, halal, kosher, other` или `""` |
| `skin_sensitivities` | `list[str]` | Аллергены/чувствительности (user-stated, НЕ клинический диагноз) |
| `prefers_flexible_cancellation` | `bool` | |
| `workplace_district` | `str` | Свободный текст, район/метро у работы |
| `home_district` | `str` | Свободный текст, район/метро у дома |
| `favorite_masters` | `list[str]` | UUID `SpecialistProfile.id` |
| `min_rating_preference` | `float` \| `null` | `0.0..5.0` |
| `busy_days` | `list[str]` | ∈ `mon, tue, wed, thu, fri, sat, sun` |

**Все поля опциональны.** Отсутствие значения = «не спрашивали» → бот трактует
как «нет предпочтения». Ни одно поле не является 🟡/🔴 — Ayla-side declared prefs
это только зелёная зона.

## Идемпотентность и порядок

- `GET` — read-only + lazy-create (идемпотентен).
- `PATCH` — last-write-wins per field; повтор → тот же результат.
- `mark-asked` / `skip` — **НЕ идемпотентны** (инкрементируют счётчики). Bot не
  должен ретраить их вслепую; ретрай = ещё один skip/cooldown-штамп.

## Что этот контракт НЕ покрывает

- Зоны 🟡/🔴, at-rest шифрование, `RedZoneAccessLog` — **на стороне бота**
  (ADR-0011/0006). Ayla их не отдаёт.
- Inferred-память (behavioral/conversational extraction) — строит бот.
- Per-field confidence persist — plug-in A1b (MemoryFact), не в этом контракте.
