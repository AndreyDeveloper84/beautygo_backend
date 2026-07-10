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
| `created_at` / `updated_at` | iso datetime | |

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

## 3. Stable-ID contract

- All ids are immutable `UUIDv4`, stable across catalog syncs.
- **Book against `specialist_service.id`.** It is the canonical bookable unit.
- **`CatalogMaster.ayla_user_id` = `user_id`** (canonical Ayla `User.id`), **not**
  `specialist` (SpecialistProfile.id). The two differ — do not conflate them.
- **Discovery / taxonomy** keys off `template` (ServiceTemplate id).
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
- 2026-07-10 — added `user_id`, `reviews_count`, `rating` to specialist-services
  payload (additive) so the bot populates `CatalogMaster.ayla_user_id` /
  `review_count` / `rating` single-call, without a join to `/internal/specialists/`
  (#1052 / #1060).
