# Multi-Tenant Architecture & Rollout

> Status: ROLLOUT (DRF-242 sub-steps 1–5 shipped 2026-04-30 → 2026-05-03)
> Owner: Andrey
> Companion docs: `BOT_NUTRITION_INTEGRATION.md` (service-to-service auth)

## Why multi-tenant

Ayla is positioned as a marketplace where multiple beauty businesses
("tenants") run on the same platform but never see each other's data.
Phase 1 had one tenant ("Формула тела") via the bot — DRF-242 puts the
plumbing in place so adding a second tenant is configuration, not a
schema migration.

## Architecture

```
                ┌──────────────────────────────────┐
       Mobile / Bot client
                │  (carries X-Tenant: <slug>)
                ▼
       ┌────────────────────┐
       │ TenantContext-     │  resolves slug → Tenant instance
       │   Middleware       │  (or None on missing/unknown header)
       │                    │  STRICT mode → 400 if header missing
       └─────────┬──────────┘    on /api/v1/* (except /auth/*)
                 │
                 │ request.tenant = Tenant(...) | None
                 ▼
       ┌────────────────────┐
       │ JWTContext-        │  resolves user from Bearer token
       │   Middleware       │
       └─────────┬──────────┘
                 │
                 │ request.user.tenant_id = <UUID> | None
                 ▼
       ┌────────────────────┐
       │ DRF view +         │  IsTenantMember compares
       │   IsTenantMember   │  request.tenant ↔ user.tenant
       │   permission       │
       └─────────┬──────────┘
                 │
                 ▼ (allowed only if tenants match)
       ┌────────────────────┐
       │ Queryset           │  filter(tenant=request.tenant)
       │   filtering        │  on every tenant-scoped surface
       └────────────────────┘
```

## Models with tenant FK

| Model | Source | FK shape | Why |
|---|---|---|---|
| `users.User` | DRF-242.3 | nullable, PROTECT, related_name="users" | Auth anchor. Single-tenant per user is enough for MVP. |
| `users.SpecialistProfile` | DRF-242.3 | nullable, PROTECT | Denormalised from `user.tenant`. Pro-app querysets filter tenant-first. |
| `ai.Conversation` | DRF-242.2 | nullable, PROTECT, db_column=`tenant_id` | Re-typed from the original DRF-240 placeholder UUIDField. Column name preserved so `conv.tenant_id` reads keep working. |
| `appointments.Appointment` | DRF-242.3 | nullable, PROTECT | Denormalised from `specialist.tenant`. Reporting stays single-table-scan. |
| `nutrition.FoodScan` | DRF-242.3 | nullable, PROTECT | Denormalised from `user.tenant`. Nutrition analytics scope by tenant first. |

`on_delete=PROTECT` everywhere: dropping a Tenant must not silently
delete user accounts, booking history, or audit trails. Admins must
reassign or soft-delete data first.

## Denormalisation invariant

```
Conversation.tenant_id  == Conversation.user.tenant_id
SpecialistProfile.tenant_id == SpecialistProfile.user.tenant_id
FoodScan.tenant_id      == FoodScan.user.tenant_id
Appointment.tenant_id   == Appointment.specialist.tenant_id
```

**Not enforced at the DB level** — keep flexibility for future cross-tenant
shares (e.g. a master who works at two locations). The `backfill_tenants`
management command initialises the invariant; new rows are expected to
maintain it via service-layer code.

## IsTenantMember permission matrix

| `request.tenant` | `user.tenant` | result |
|---|---|---|
| None | any | True (permissive — auth/legacy paths) |
| T1 | None | **False** (un-backfilled user blocked) |
| T1 | T1 | True |
| T1 | T2 | **False** (escalation attempt) |

Anonymous (no JWT) → False unconditionally. Combine with
`IsAuthenticated` to express "must-be-logged-in AND match-tenant".

## Rollout (strict-mode flip)

Strict mode is gated by `MULTI_TENANT_STRICT` env var (default `false`).

### Pre-flight

1. Run `manage.py backfill_tenants --dry-run` on the target env. Verify
   row counts match expectations.
2. Run without `--dry-run`. Idempotent — safe to re-run.
3. Verify: `User.objects.filter(tenant__isnull=True, is_active=True).count() == 0`.
4. Verify: same for SpecialistProfile / Conversation / FoodScan / Appointment.

### Flip per environment

```
dev    → MULTI_TENANT_STRICT=true   → smoke /api/v1/* with valid X-Tenant
staging → same → 24h soak
prod   → same
```

Each step needs a worker restart (env var read at boot).

### Rollback

```
MULTI_TENANT_STRICT=false
restart workers
```

No migration rollback needed — the schema is identical between modes.
The flip only changes whether a missing `X-Tenant` header on
`/api/v1/*` returns 400 (strict) or passes through to permission layer
(permissive).

### What strict mode changes

| Behaviour | Permissive (current default) | Strict |
|---|---|---|
| Missing `X-Tenant` on `/api/v1/specialists/` | Passes through; `IsTenantMember` permissive (None). Queryset must filter by tenant manually. | **400 TENANT_REQUIRED** before view runs. |
| Unknown slug | `request.tenant=None` → `IsTenantMember` permissive | 400 |
| `/api/v1/auth/login/` without header | Passes | Passes (opt-out — registration is pre-tenant) |
| `/api/v1/health/` without header | Passes | Passes (excluded path) |
| `/api/v1/nutrition/internal/scan/` (bot) | Passes (excluded path; uses `X-Service-Token` instead) | Passes |

## Service-to-service paths (bot)

The MAX bot does NOT carry an `X-Tenant` header. It calls
`/api/v1/nutrition/internal/*` with `X-Service-Token` + `X-External-User-ID`
(see `BOT_NUTRITION_INTEGRATION.md`). Internal paths are in
`TenantContextMiddleware.EXCLUDED_PATH_PREFIXES` so strict mode never
blocks them.

Each ProxyUser (`bot:{max_user_id}`) gets `tenant=None` by default.
DRF-242.5 backfill assigns the default tenant — Phase C migration will
later swap this to the real Ayla User when the bot user registers.

## Excluded paths from middleware

```
/admin/                        — Django admin, session auth, no tenant scope
/api/schema/, /api/docs/       — OpenAPI surfaces
/api/redoc/
/api/v1/health/                — load balancer probes
/api/v1/nutrition/internal/    — service-to-service (DRF-246/247/248)
/static/, /media/              — file serving
```

In strict mode, additionally opted out:

```
/api/v1/auth/                  — pre-tenant registration handshake
```

## Runbook: adding a new tenant

1. `python manage.py shell -c "from tenants.models import Tenant; Tenant.objects.create(slug='newco', name='New Co')"`
2. New users register via `/api/v1/auth/register/` → assigned to `newco` by service-layer code (DRF-242.6+ — not yet implemented; for now use admin to set `User.tenant`).
3. Mobile client sends `X-Tenant: newco` on every `/api/v1/*` request.
4. SpecialistProfile / Appointment / FoodScan / Conversation inherit from `user.tenant` via service-layer save() validators (DRF-242.6+ — not yet implemented; backfill handles existing rows).

## Pending follow-ups (post-DRF-242)

- **DRF-242.6** (not ticketed) — service-layer save() validators that
  enforce the denormalisation invariant on new rows. Until then, any
  service code that creates these models MUST set `tenant` explicitly
  to match the parent.
- **DRF-242.7** (not ticketed) — viewset-level queryset filtering helper
  (`TenantScopedViewSetMixin`) so queries auto-`.filter(tenant=request.tenant)`.
- **Phase C** — bot ProxyUser → real Ayla User migration. `User.linked_proxy_id`
  + `/api/v1/auth/link-proxy/` endpoint.
- **Tenant subscription / billing** — separate model under `tenants/`.

## References

- `users/middleware.py::TenantContextMiddleware`
- `users/permissions.py::IsTenantMember`
- `tenants/management/commands/backfill_tenants.py`
- Linear: DRF-242 (umbrella), 242.1–242.5 sub-tickets
- Companion: `BOT_NUTRITION_INTEGRATION.md`
