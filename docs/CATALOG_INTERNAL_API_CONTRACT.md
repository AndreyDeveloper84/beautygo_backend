# Catalog Internal API Contract — S3A (2026-07)

**Audience:** the Ayla bot **S3B** mirror agent (`ai-bot-platform#1044`).
**Epic:** `AndreyDeveloper84/ai-bot-platform#1044` + Ayla `#200`.
**Design:** `docs/CATALOG_DOMAIN_REBUILD_S3_DESIGN_2026-07.md`.
**Status:** stable for STAGE 2. Read-only mirror; write flow (draft/confirm)
is S3C and is **not** part of this contract.

This is the authoritative wire contract for the new canonical catalog layer
(`SalonService` → `SpecialistService`). The legacy `Service` mirror at
`/api/v1/internal/services/` is unchanged and remains available during the
strangler-fig transition.

---

## Auth

Service-to-service Bearer, no mobile JWT, no `X-App-Type`:

```
Authorization: Bearer <AYLA_INTERNAL_API_TOKEN>
```

- Wrong / missing bearer → **403**. Empty server-side token fails closed → 403.
- `request.user` stays Anonymous (`IsInternalBearer`).

## Conventions

- Base path: `/api/v1/internal/catalog/`.
- **List** responses use the project default pagination (`PageNumberPagination`,
  `PAGE_SIZE=20`, `?page_size=` up to 100):

  ```json
  { "count": 1, "next": null, "previous": null, "results": [ { ... } ] }
  ```
- **Detail** responses return the object directly (no pagination wrapper).
- Filtering via query params (django-filter, exact match).
- UUIDs are strings; `DecimalField` serializes as a string (e.g. `"1500.00"`);
  datetimes are ISO-8601; FK fields serialize as the related UUID string.

---

## 1. Salon services (mid layer)

`GET /api/v1/internal/catalog/salon-services/`
`GET /api/v1/internal/catalog/salon-services/{id}/`

**Filters:** `tenant`, `template`, `is_active`.

| field | type | notes |
|---|---|---|
| `id` | uuid str | **stable** SalonService id |
| `tenant` | uuid str | salon (Tenant) |
| `template` | uuid str \| null | ServiceTemplate (taxonomy); null = off-taxonomy custom |
| `category` | uuid str \| null | ServiceCategory |
| `name` | str | |
| `duration_minutes` | int \| null | salon-level default (null ⇒ resolves from template) |
| `base_price` | str \| null | indicative salon price; per-specialist price on the bookable |
| `requires_health_check` | bool | salon-level flag (escalate-only vs template floor) |
| `is_active` | bool | |
| `source` | str | `manual` \| `yclients` \| `seed` |
| `goals` | list[obj] | **DRF-1308, additive 2026-08-23.** Цели, уже разрешённые по дереву категорий. Элемент: `{"key": str, "label": str}`, порядок — `GoalOption.sort_order`. `[]` = цель не заявлена (не ошибка) |
| `created_at` / `updated_at` | iso datetime | |

### `goals` — как разрешается (DRF-1308)

Разрешение делает Ayla, а не зеркало: у бота **нет таблицы категорий**, он
хранит UUID категории в `raw` и обойти дерево не может. ADR-0009: зеркало —
read-replica, а не источник.

Порядок разрешения, ровно как в решении владельца от 23.08:

1. связи цели на **собственной категории** услуги;
2. если пусто — на **ближайшем предке** этой категории со связью (п. 1
   решения: parent fallback). Дерево не глубже двух уровней
   (`ServiceCategory.clean()`);
3. если пусто — та же цепочка от **категории канонического шаблона**
   (`template.category`). Собственная категория салона может быть
   коммерческой витриной («Комплексные программы и пакеты»), шаблон —
   предметной веткой канона;
4. если пусто — `[]`. Ложную цель ради покрытия не подставляем (п. 4
   решения владельца). Пустой список — валидное состояние.

Именно fallback, а не объединение источников: своя связь, если она есть,
побеждает. Замер на dev-контуре 23.08 — у 32 услуг цели выводятся обоими
путями и **расходятся в 0 случаях**, ещё у 3 выводятся только через шаблон.

Реализация: `services/goal_resolution.py`. Обратное направление
(«цель → категории», для фильтрации выдачи) — `goals/resolution.py`.

Example detail:

```json
{
  "id": "6f1c2e9a-....",
  "tenant": "b0a1....",
  "template": "9d3f....",
  "category": "1122....",
  "name": "Классический маникюр",
  "duration_minutes": null,
  "base_price": null,
  "requires_health_check": false,
  "is_active": true,
  "source": "manual",
  "goals": [
    {"key": "self_care", "label": "Привести себя в порядок"}
  ],
  "created_at": "2026-07-09T18:31:00Z",
  "updated_at": "2026-07-09T18:31:00Z"
}
```

---

## 2. Specialist services — **BOOKABLE** (the booking key)

`GET /api/v1/internal/catalog/specialist-services/`
`GET /api/v1/internal/catalog/specialist-services/{id}/`

**Filters:** `tenant`, `specialist`, `salon_service`, `is_active`.

| field | type | notes |
|---|---|---|
| `id` | uuid str | **🔑 stable booking key** — the id the bot books against |
| `salon_service` | uuid str | parent SalonService |
| `specialist` | uuid str | **SpecialistProfile.id** (profile pk — not the User id) |
| `user_id` | uuid str | **canonical Ayla `User.id`** — map to `CatalogMaster.ayla_user_id`. NOT the same as `specialist`. |
| `tenant` | uuid str \| null | denormalized salon |
| `template` | uuid str \| null | taxonomy id (via salon_service.template) — discovery key |
| `name` | str | service name (from salon_service) — **C6 link key**, raw (bot normalizes) |
| `category_slug` | str | category slug (via salon_service.category) — **C6 link key** |
| `duration_minutes` | int \| null | raw specialist override (may be null) |
| `resolved_duration` | int \| null | **effective** duration: specialist → salon → template. Non-null on an active bookable. **Use this.** |
| `requires_health_check` | bool | raw specialist flag |
| `resolved_requires_health_check` | bool | **effective** health gate: OR across template floor, salon, specialist (escalate-only, D1). **Use this** to decide whether to run the health check before booking. |
| `price` | str | specialist price |
| `buffer_after_minutes` | int | required gap after the service |
| `is_active` | bool | bookability |
| `yclients_staff_id` | str | YClients staff id from SpecialistProfile (`""` if none) — cross-source reconciliation |
| `reviews_count` | int | from SpecialistProfile → `CatalogMaster.review_count` (single-call populate, #1060) |
| `rating` | str | from SpecialistProfile → `CatalogMaster.rating` |
| `created_at` / `updated_at` | iso datetime | |

Example detail:

```json
{
  "id": "a4e0....",
  "salon_service": "6f1c....",
  "specialist": "77aa....",
  "user_id": "33cc....",
  "tenant": "b0a1....",
  "template": "9d3f....",
  "name": "Маникюр классический",
  "category_slug": "manicure",
  "duration_minutes": 45,
  "resolved_duration": 45,
  "requires_health_check": false,
  "resolved_requires_health_check": true,
  "price": "1500.00",
  "buffer_after_minutes": 0,
  "is_active": true,
  "yclients_staff_id": "9001",
  "reviews_count": 17,
  "rating": "4.7",
  "created_at": "2026-07-09T18:31:00Z",
  "updated_at": "2026-07-09T18:31:00Z"
}
```

> In the example, `resolved_requires_health_check` is `true` even though the
> specialist row's `requires_health_check` is `false`, because the underlying
> `ServiceTemplate` sets the floor. Always trust the `resolved_*` fields.

---

## 2a. Specialists — the masters mirror (`?tenant=` since DRF-1313)

`GET /api/v1/internal/specialists/`
`GET /api/v1/internal/specialists/{id}/`

This handle lives outside `/api/v1/internal/catalog/` (it predates S3A, #1016 S2)
but the masters mirror reads it, so its tenant contract is pinned here.

**Filters:** `tenant` (uuid), plus the public catalog filters
(`is_available`, `min_rating`, `service_id`, `category_id`, `min_price`,
`max_price`).

| field | type | notes |
|---|---|---|
| `id` | uuid str | `SpecialistProfile.id` — the key `CatalogMaster.id` mirrors |
| `tenant` | uuid str \| null | **owning salon.** Added DRF-1313; `null` only for a profile with no tenant assigned |
| … | | remaining fields unchanged — the public specialist card / detail payload |

### `tenant` is optional, and that is deliberate

`/api/v1/internal/` is excluded from `TenantContextMiddleware`, so `request.tenant`
is always `None` here — the tenant cannot be derived and must be named. Omitting
`?tenant=` still returns **every active master on the platform** and logs a
`WARNING` (`internal.specialists.list_without_tenant`).

**A catalog mirror must always send it.** Without it every syncing tenant pulls
the same roster and the first one to sync claims all of it. On 2026-08-23 that
put the five masters of four salons under `mkt-spatrium` and left three of five
pilot salons with services, no masters and no bookable edges (DRF-1313).

A malformed uuid is a **400**, not an ignored filter. Answering a typo with the
whole platform is the same failure wearing a different hat.

### Verify, don't trust

Every row states its own `tenant`. The consumer should re-check it before
writing — `upsert_specialists` skips a row whose payload tenant differs from the
tenant being synced, exactly as `upsert_master_services` already does for edges
(§2). A `null` means *unverifiable*, not *foreign*, and must not be blocked.

---

## 2b. Salon readiness — по мастерам поимённо (DRF-2117, §50 п.4)

```
GET /api/v1/internal/salons/<slug>/readiness/
Authorization: Bearer <AYLA_INTERNAL_API_TOKEN>
X-External-User-ID: <actor>        # владелец / администратор, от чьего имени читает бот
```

Сторож — `users.permissions.IsInternalBearerForSalonSubject` (подкласс
субъектного сторожа DRF-1617): runtime-bearer (provisioning-credential →
отказ), заголовок обязателен, актор резолвится **без создания строк**, должен
быть активен и держать активную TUR `admin` в активном тенанте `<slug>`.
Чужой, неизвестный или выключенный салон → **404** (slug чужого тенанта не
подтверждается, DRF-1036), не 403. Перепись —
`users/tests/test_internal_subject_authorization.py` (салонные маршруты),
отрицательные тесты — `tenants/tests/test_salon_readiness_2117.py::TestSubject`.

**Что это за половина.** Каталог НЕ знает §83 (`schedule_confirmed_at`),
`catalog_specialist_id`, `SoloIdentityLink` и `MasterService.sellable` — эти
столбцы живут в зеркале бота (`apps/catalog/master_state.py::sale_block`).
Бот накладывает их на этот ответ сам; `ready` здесь означает «каталог не
видит препятствий», и бот вправе сузить. Список у бота **длиннее при
включённом `MASTER_SCHEDULE_CONFIRMATION_REQUIRED`** (§83 → `schedule_unconfirmed`);
здесь `schedule` — только «есть рабочий день с началом и концом».

```jsonc
200 {
  "data": {
    "salon": {"slug": "formula-tela", "name": "Формула тела"},
    "ready": false,                     // только при пустом problems И без unknown
    "checked_at": "2026-09-20T09:00:00+00:00",
    "horizon_days": 7,
    "masters": [
      {
        "id": "<SpecialistProfile.id>",  // = CatalogMaster.catalog_specialist_id в боте
        "user_id": "<User.id>",          // = CatalogMaster.ayla_user_id в боте
        "name": "Анна",                  // первое слово display_name
        "checks": {                      // ok | problem | unknown | skipped
          "publication": "ok", "schedule": "problem", "services": "ok",
          "catalog_link": "ok", "slots": "skipped"
        },
        "problems": [{"code": "schedule_missing", "text": "Анна — не настроен график"}]
      }
    ],
    "problems": [                        // плоский список по всем мастерам, в порядке masters
      {"master": {"id": "…", "name": "Анна"}, "code": "schedule_missing",
       "text": "Анна — не настроен график"}
    ],
    "limits": ["…"]                      // названные пределы, как есть
  }
}
```

| check | code | когда |
|---|---|---|
| publication | `not_published` | профиль не опубликован (`users.sellable.is_published`) |
| publication | `hidden_from_catalog` | опубликован, `is_available=false` |
| publication | `booking_paused` | опубликован, `is_booking_enabled=false` |
| schedule | `schedule_missing` | нет рабочего дня с началом и концом (`SpecialistWorkingHours`) |
| services | `services_missing` | ни одного продаваемого ребра этого тенанта (`services.offer_sellable.sellable_offer_q`: активно, услуга активна, цена ≥ 1 ₽) |
| services | `service_duration_missing` | у первого продаваемого ребра нет длительности (услуга / ребро / шаблон) — путь записи такую откажет |
| catalog_link | `identity_not_linked` | MAX-личность не привязана к аккаунту мастера (`users.publication.is_linked`) |
| slots | `no_free_slots` | ни одного окна на `horizon_days` дней (`AvailabilityQueryService`, по длительности первой продаваемой услуги) |
| slots | `slots_unknown` | вычислитель упал — **это проблема**, класс исключения в логе `salon_readiness.slots_unknown` |
| салон | `no_masters` | ни одного мастера — `problems[].master = null`, `ready=false` |

`slots` = `skipped`, пока `schedule` или `services` не `ok` (окон нет по
построению; второе слово о том же — шум). `unknown` никогда не читается как
«ок»: `ready=false`. Тексты — константы по коду (`tenants/salon_readiness.py::TEXTS`),
бот держит свою таблицу по тем же кодам; имена без склонений («Анна — …»,
предел назван).

Мастера — `SpecialistProfile` тенанта с активным неудалённым аккаунтом, в
порядке `display_name`. Салон без мастеров — `masters: []`, `ready: false`,
одна проблема уровня салона `no_masters` с `master: null` (три мастера с
выключенными аккаунтами — это ноль мастеров, и «готов» тут был бы ложью).

**Предел слотов (до DRF-1637):** только «есть/нет», одна услуга на мастера,
7 дней от сегодня в поясе мастера, горизонт брони `booking_horizon_end()`.
Стоимость — до 7 вычислений окон на мастера за вызов: это кнопка, не цикл.
Кэш окон 60 с (`SlotCacheService`) — правка графика видна не сразу.

## 3. Stable-ID contract

- All ids are immutable `UUIDv4`, stable across catalog syncs.
- **Book against `specialist_service.id`.** It is the canonical bookable unit.
- **`CatalogMaster.ayla_user_id` = `user_id`** (canonical Ayla `User.id`), **not**
  `specialist` (SpecialistProfile.id). The two differ — do not conflate them.
- **Discovery / taxonomy** keys off `template` (ServiceTemplate id).
- **Catalog link (C6, orchestrator decision 2026-07-19):** the bot links its
  catalog rows to Ayla services by `(category_slug, normalized name)` with
  `resolved_duration` as tiebreaker. `ServiceTemplate` intentionally has **no
  `slug`** field (no matching-only migration in the pilot). Normalization
  rules, fixed for both sides: `lower`, `trim`, `ё→е`, collapse whitespace,
  strip `«»` quotes. Ayla exposes raw `name` + `category_slug`; normalization
  and the exception mapping file live bot-side (W3).
- Idempotency across YClients re-exports is guaranteed Ayla-side by
  `ExternalSourceMapping (source, external_type, external_id, tenant) → Ayla id`
  (keyed by YClients `service_id` / `staff_id`). A re-import re-uses the same
  Ayla id — the bot never needs to re-map a service whose id it already holds.

## 4. Out of scope (later chunks)

- Draft/confirm **write** endpoints — S3C intake (YClients API-pull primary,
  CSV bootstrap fallback).
- External-busy / slot guard — S3-CAL (Variant B YClients webhook).
- `Appointment` booking against `SpecialistService` — S3-CUT (founder-authorized).

## 5. Change policy

Additive-only within S3A. New fields may be appended; existing field names /
types will not change without bumping this contract and notifying S3B.

**Changelog**
- 2026-09-20 — `GET /api/v1/internal/salons/<slug>/readiness/` (DRF-2117, §2b): салонная
  готовность поимённо под субъектным сторожем салона; чужой салон → 404.
- 2026-08-23 — `/api/v1/internal/specialists/` accepts `?tenant=<uuid>` and every
  row now carries `tenant` (both additive). The masters mirror was the only one of
  the three catalog pulls without a tenant scope; see §2a (DRF-1313).
- 2026-07-19 — added `name`, `category_slug` to specialist-services payload
  (additive) — C6 catalog-link keys: the bot matches
  `(category_slug, normalized name)` + duration tiebreaker; no
  `ServiceTemplate.slug` per orchestrator decision.
- 2026-07-10 — added `user_id`, `reviews_count`, `rating` to specialist-services
  payload (additive) so the bot populates `CatalogMaster.ayla_user_id` /
  `review_count` / `rating` single-call, without a join to `/internal/specialists/`
  (#1052 / #1060).
