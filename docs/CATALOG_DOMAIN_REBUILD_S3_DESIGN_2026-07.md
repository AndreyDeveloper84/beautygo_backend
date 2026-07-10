# Catalog Domain Rebuild — S3 Design (2026-07)

**Epic:** `AndreyDeveloper84/ai-bot-platform#1044` (bot) + Ayla `#200` (backend).
**Branch:** `feat/1044-catalog-domain-rebuild` (off `origin/dev`).
**Domain:** `services/`.
**Status:** design approved 2026-07-09 (all 5 forks). Implementation starts S3A.1.

---

## 1. Purpose & scope ruling

Build a first-class canonical catalog domain in Ayla — **not** a coverage bridge over the
deprecated `mysite`/`#1043` matcher. The canonical shape is a strict 3-layer chain:

```
ServiceTemplate  (taxonomy, ≠ bookable)           ← exists (#201)
      │  nullable FK (off-taxonomy custom allowed)
      ▼
SalonService     (a salon offers this service)     ← NEW
      │
      ▼
SpecialistService (BOOKABLE — a master performs it) ← NEW
```

Plus onboarding + external-intake support: **DraftSalonService**, **ExternalSourceMapping**.

### Strangler-fig, additive (approved)

"Полный домен-ребилд (НЕ мост)" ≠ dropping the existing `services.Service` in one shot.
`Service` is load-bearing (`appointments.Appointment → Service`, payments read it). We use
expand/contract:

1. **S3A (this stream):** build the new domain **additively**, alongside `Service`.
   `Service`/`Appointment` are **not touched**.
2. **S3-CUT (separate, later, out of scope here):** cut over `Appointment` to
   `SpecialistService`. Pilot-critical, on the G-Booking path, **requires founder
   authorization** (crosses the forbidden zone). Risk guidance: add a **nullable
   `specialist_service` FK to Appointment** additively (new bookings write it); do **not**
   hard-repoint the existing `service` FK. Coordinated with booking-stream (`#1016`/S2) +
   payments. Detail deferred to S3-CUT scoping.

### Domain facts (verified against `origin/dev`)

- **"Salon" = `tenants.Tenant`** — there is no separate Salon model.
- Existing bookable = `services.Service` (FK `specialist` + `category` + `tenant`).
- `users.SpecialistProfile` already carries YClients hooks: `booking_source`
  (`ayla_local`/`yclients`), `yclients_company_id`, `yclients_staff_id`
  (ADR-0009 §Booking SoR, `#439`).
- `ServiceTemplate` (taxonomy, nullable durations, `requires_health_check`,
  `contraindications`) already seeded on dev (`#201`).

---

## 2. Entity model (all NEW tables, additive)

All PKs are `UUIDField(primary_key=True, default=uuid.uuid4, editable=False)` — the repo
convention (no BaseModel). All FKs to tenant use `on_delete=PROTECT` (matches
`ServiceCategory.tenant`).

### 2.1 `SalonService` — a salon offers a service

| field | type | notes |
|---|---|---|
| `id` | UUID PK | stable |
| `tenant` | FK `tenants.Tenant`, PROTECT | the salon |
| `template` | FK `ServiceTemplate`, PROTECT, **null=True, blank=True** | link to taxonomy; null ⇒ off-taxonomy custom (D2) |
| `category` | FK `ServiceCategory`, SET_NULL, null=True | denormalized for marketplace filters; defaults from `template.category` |
| `name` | CharField(200) | display; defaults from template on materialization |
| `duration_minutes` | PositiveInteger, null=True | salon-level default; null ⇒ resolves from template (D1) |
| `base_price` | Decimal(10,2), null=True | salon-level indicative price; per-specialist price lives on SpecialistService |
| `requires_health_check` | Boolean, default=False | **escalate-only** vs template floor (D1) |
| `is_active` | Boolean, default=True | |
| `source` | CharField choices `manual`/`yclients`/`seed`, default `manual` | provenance |
| `created_at`/`updated_at` | auto | |

Constraints: `unique_together (tenant, template, name)`.
Indexes: `(tenant, is_active)`, `(tenant, category, is_active)`.
`clean()`: if `template` is null then `category` is required (D2).

### 2.2 `SpecialistService` — BOOKABLE

| field | type | notes |
|---|---|---|
| `id` | UUID PK | **stable booking key the bot resolves** |
| `salon_service` | FK `SalonService`, PROTECT, related_name `specialist_services` | |
| `specialist` | FK `users.SpecialistProfile`, CASCADE, related_name `specialist_services` | |
| `tenant` | FK `tenants.Tenant`, PROTECT, null=True | denormalized from salon_service/specialist for scoping |
| `duration_minutes` | PositiveInteger, **null=True in DB** | resolved value; **must be non-null when `is_active=True`** (enforced in `clean()` + resolution helper) — DoD "resolved duration" |
| `price` | Decimal(10,2), MinValue 1 | specialist price |
| `requires_health_check` | Boolean, default=False | resolved, escalate-only floor from template/salon (D1) |
| `buffer_after_minutes` | PositiveSmallInteger, default=0 | parity with `Service` |
| `is_active` | Boolean, default=True | bookability toggle |
| `created_at`/`updated_at` | auto | |

Constraints: `unique_together (specialist, salon_service)`.
Indexes: `(tenant, is_active)`, `(specialist, is_active)`, `(salon_service, is_active)`.

**Resolution helpers** (single source of truth, used by serializers + `clean()`):
- `resolved_duration()` = first non-null of `self.duration_minutes` →
  `salon_service.duration_minutes` → `salon_service.template.duration_default`.
- `resolved_requires_health_check()` = **OR** of template floor, salon flag, specialist flag
  (D1 escalate-only: if any layer requires it, it is required; downstream cannot relax it).
- `clean()`: if `is_active` and `resolved_duration()` is None → ValidationError (a bookable
  service must have a resolvable duration).

### 2.3 `DraftSalonService` — onboarding "Confirm, don't create"

External prefill lands here first; a human confirms → materializes a `SalonService`
(+ `ExternalSourceMapping`). **The confirm/reject WRITE flow is S3C** — S3A ships the model
+ admin + state field only.

| field | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant` | FK `tenants.Tenant`, PROTECT | target salon |
| `status` | CharField choices `pending`/`confirmed`/`rejected`/`superseded`, default `pending` | |
| `external_source` | CharField choices `yclients`/`csv`, default `yclients` | **transport channel** (API-pull vs CSV-bootstrap). System-of-record is YClients in both ⇒ the resulting `ExternalSourceMapping.source` is `yclients` regardless. |
| `external_service_id` | CharField(64), blank | YClients service id (blank for pure-manual drafts) |
| `suggested_template` | FK `ServiceTemplate`, SET_NULL, null=True | fuzzy-matched taxonomy suggestion |
| `external_name` | CharField(200) | raw name from source |
| `suggested_duration` | PositiveInteger, null=True | |
| `suggested_price` | Decimal(10,2), null=True | |
| `raw_payload` | JSONField, default=dict | original source record (audit / re-map) |
| `confirmed_salon_service` | FK `SalonService`, SET_NULL, null=True | set on confirm |
| `confirmed_at` | DateTime, null=True | |
| `confirmed_by` | FK `users.User`, SET_NULL, null=True | |
| `created_at`/`updated_at` | auto | |

Constraints: `unique_together (tenant, external_source, external_service_id)` — but only
enforced when `external_service_id` is non-blank (conditional `UniqueConstraint` with a
`condition`), so multiple blank/manual drafts coexist.
Indexes: `(tenant, status)`.

### 2.4 `ExternalSourceMapping` — idempotent external↔Ayla key

| field | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `source` | CharField choices `yclients`, default `yclients` | extensible |
| `external_type` | CharField choices `service`/`staff` | discriminates target FK |
| `external_id` | CharField(64) | YClients `service_id` or `staff_id` |
| `tenant` | FK `tenants.Tenant`, PROTECT | YClients ids are per-company ⇒ scope required |
| `salon_service` | FK `SalonService`, CASCADE, null=True | set when `external_type='service'` |
| `specialist` | FK `users.SpecialistProfile`, CASCADE, null=True | set when `external_type='staff'` |
| `created_at`/`updated_at` | auto | |

Constraints: `unique_together (source, external_type, external_id, tenant)` — **the
idempotency key**.
`clean()`: exactly one of `salon_service`/`specialist` set, matching `external_type`.
Design choice (D6): two explicit nullable FKs, **not** a GenericForeignKey — simpler,
type-discriminated, indexable.

Re-import guarantee: a repeated YClients export re-uses the mapped Ayla id
(upsert on the unique key), never mints a new one.

---

## 3. 🔑 Stable-ID contract (bot S3B STAGE 2 + discovery depend on this)

- Every catalog entity exposes an immutable `UUIDv4` PK, stable across syncs.
- **Bot booking key = `SpecialistService.id`** (the bookable unit).
- **Discovery / taxonomy key = `ServiceTemplate.id`.**
- Idempotency across YClients re-exports is guaranteed by
  `ExternalSourceMapping(source, external_type, external_id, tenant) → Ayla id`.
- The authoritative wire contract (endpoints, field-by-field JSON, examples) ships as a
  **separate contract doc in PR-B** (`docs/CATALOG_INTERNAL_API_CONTRACT.md`) and is the
  artifact handed to the bot S3B agent.

---

## 4. Internal API (S3A = READ mirror only; D3)

Follows the existing pattern (`services/internal_api.py`: reuse a viewset, swap auth to
`users.permissions.IsInternalBearer`, empty `authentication_classes`; envelope via
`core.pagination.paginated_success_response` / `users.response.success_response`).

New Bearer read endpoints, mounted under `/api/v1/internal/catalog/`:

- `GET /api/v1/internal/catalog/salon-services/` (+ `/{id}/`)
  — filters: `tenant`, `template`, `is_active`. Serializer exposes resolved fields,
  `template` id, `category`, and linked `ExternalSourceMapping` external ids.
- `GET /api/v1/internal/catalog/specialist-services/` (+ `/{id}/`)
  — the bookable list; filters: `tenant`, `specialist`, `salon_service`, `is_active`.
  Serializer exposes **resolved** `duration_minutes` + `requires_health_check`, stable `id`,
  `specialist` id, and YClients ids (via SpecialistProfile / mapping).

The existing legacy `/api/v1/internal/services/` (Service mirror) stays intact.
**Draft/confirm WRITE endpoints are NOT in S3A — they are S3C.**

---

## 5. Implementation chunks (per-chunk PR → `dev`)

### PR-A — S3A.1: models + migration + admin + model-tests
- 4 new models in `services/models.py` (+ resolution helpers, `clean()` rules).
- One additive migration `0012_*` (new tables + indexes/constraints only; nothing on
  `Service`/`Appointment`).
- `services/admin.py` registrations. **D2 nuance:** SalonService/SpecialistService admin
  surfaces `requires_health_check` as an editable field so ops can set custom health-check on
  off-taxonomy / salon-specific services.
- TDD model tests: duration/health resolution cascade, escalate-only floor, active-bookable
  duration invariant, ExternalSourceMapping idempotency uniqueness + `clean()` XOR, Draft
  conditional unique, constraint coverage.

### PR-B — S3A.2: internal API + contract doc
- `serializers.py`: `SalonServiceInternalSerializer`, `SpecialistServiceInternalSerializer`
  (resolved fields).
- `internal_api.py` + `internal_urls.py`: new viewsets + filters, Bearer auth.
- Wire route under `/api/v1/internal/catalog/` in `djangoProject/urls.py`.
- TDD API tests: Bearer boundary (401 without token), tenant filtering, resolved-field
  correctness, stable-id presence, pagination envelope.
- **`docs/CATALOG_INTERNAL_API_CONTRACT.md`** — the artifact for bot S3B STAGE 2.

---

## 6. Forward-look (not S3A — recorded so S3A shape supports it)

- **S3C intake** (approved D5): **YClients API-pull is primary** (pilot salon creds will be
  provided; use `yclients_company_id`/`yclients_staff_id` already on SpecialistProfile).
  **CSV-bootstrap is a fallback only** if creds aren't ready for the first Penza load. Both
  feed the **same DraftSalonService pipeline** (external prefill → draft → human confirm →
  SalonService + ExternalSourceMapping). S3C may be designed/scaffolded against the YClients
  API contract immediately; live end-to-end pull waits on creds (does **not** block S3A).
- **S3-CAL:** Variant B — YClients webhook → Ayla busy guard + recheck-at-confirm
  (no double-booking). Reads slots-busy only; does not modify appointments logic.
- **S3D:** contract tests.
- **S3-CUT:** Appointment → SpecialistService cutover (founder-authorized, separate).

## 7. Out of scope / guardrails
- Do **not** touch `payments`, `users`, `appointments` logic (read-only slots-busy for CAL).
- Stay in `services/**` (+ `internal_api`, migrations, tests) and the route wiring in
  `djangoProject/urls.py`.
- Bot mirror re-key (S3B) is a **separate agent**.
- TDD, mypy (incl. tests), no `git add .`/`-A`, Conventional Commits, per-chunk PR → `dev`,
  PR body `Refs AndreyDeveloper84/ai-bot-platform#1044` + `#200`.

## 8. Open item on radar (non-blocking)
- Timing of YClients pilot creds determines when S3C can be tested live. S3A does not depend
  on it.
