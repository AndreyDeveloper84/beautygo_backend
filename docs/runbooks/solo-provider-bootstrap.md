# Solo provider bootstrap runbook

> **Operational procedure for onboarding a self-employed pilot
> provider** (e.g. Ольга, pilot persona #1 in Пенза). Per
> `project_pilot_scope_discipline` and ADR-0009 §5.
>
> **Ownership boundary:** the atomic creation transaction is owned by
> the bot-platform W4 service (`apps/identity/services/solo_onboarding.py`).
> Alpha (Ayla djangoproject) owns the canonical destination state —
> `Tenant`, `User`, `TenantUserRelationship`, `SpecialistProfile`,
> `Service`, `SpecialistWorkingHours`. This runbook is for **ops**:
> verifying the W4 cascade landed correctly + recovering manually if
> it didn't.

## Pre-requisites

Before the first solo provider onboarding:

- [ ] Ayla djangoproject + bot-platform deployed to staging VPS with
      matching `dev` HEAD.
- [ ] `AYLA_INTERNAL_API_TOKEN` env var set on both sides (bot-platform
      → Ayla service auth). Rotated < 30 days.
- [ ] `YOOKASSA_*` env vars set (see
      `docs/runbooks/webhook-readiness-2026-05-26.md`).
- [ ] At least one bot channel token configured (MAX bot for pilot —
      verify with bot-platform team).
- [ ] Smoke endpoints respond:
  ```
  curl -fsS https://<vps>/api/v1/health/
  curl -fsS https://<vps>/api/v1/health/ready/
  ```

## The 10-step checklist

Founder pilot_scope_discipline mandates a single User holds three
roles (owner + admin + master) in one tenant — the "self-employed
solo provider" archetype per `project_solo_provider_universal_ui`.

W4's `solo_onboarding.py` performs steps 1-6 atomically. Steps 7-10
are partly bot-platform / mobile / smoke domains; verification only
on the Ayla side.

### Step 1 — Tenant created

After W4 cascade fires, expect ONE new `Tenant` row, `is_active=True`,
slug ∈ {salon-friendly slug}:

```sql
-- Run on Ayla djangoproject DB
SELECT id, slug, name, is_active, created_at
FROM tenants_tenant
ORDER BY created_at DESC
LIMIT 5;
```

Recovery if missing: W4 transaction rolled back — re-run W4 onboarding
flow from bot-platform side. Do NOT create Tenant manually; the
W4-side outbox events would never fire.

### Step 2 — User created

The provider's canonical Ayla `User` row exists. For MAX bot pilot
the username will look like `bot:<max_user_id>` (proxy User created
on first bot interaction via `users.services.resolve_external_user`).

```sql
SELECT id, username, role, phone, is_proxy, is_verified, created_at
FROM users_user
WHERE username = 'bot:<max_user_id>';
```

Expected: `role='client'`, `is_proxy=True` initially. Phone is
typically empty until provider verifies via OTP from the Mini App.

Recovery: re-trigger first bot message to recreate the proxy — W4
proxy creation is idempotent.

### Step 3 — TenantUserRelationship: 3 active rows (owner + admin + staff)

Per ADR-0008, a solo provider is represented as multi-role with
**separate active TUR rows** for each role. Schema β
(partial unique active) supports this because
`tur_unique_active` is on `(user, tenant, role)`-tuple — three
distinct rows.

```sql
SELECT id, user_id, tenant_id, role, is_active, granted_at, granted_by
FROM users_tenantuserrelationship
WHERE user_id = '<provider_user_id>'
  AND tenant_id = '<tenant_id>'
  AND is_active = true
ORDER BY granted_at;
```

Expected: exactly 3 rows with `role ∈ {customer, staff, admin}`,
all `is_active=True`, `granted_by='system'`.

> **NOTE — schema check.** Current Ayla TUR partial unique constraint
> `tur_unique_active` is on `(user, tenant)` (one active TUR per
> pair), NOT on `(user, tenant, role)`. Per
> `feedback_adr_0009_ownership_verification`, the multi-role expansion
> may live in bot-platform's `TenantStaff` table instead. Verify with
> bot-platform team where multi-role rows actually land for the
> pilot. If Alpha's TUR is single-role-per-pair, only ONE TUR row
> appears here and the operational roles live bot-platform-side.

Recovery: if any role is missing/inactive, do NOT manually flip rows.
Re-run W4 cascade; it's idempotent and re-grants only missing roles.

### Step 4 — SpecialistProfile created

```sql
SELECT id, user_id, tenant_id, display_name, status,
       is_available, is_booking_enabled
FROM users_specialistprofile
WHERE user_id = '<provider_user_id>';
```

Expected: one row, `status='active'`, `is_available=True`,
`is_booking_enabled=True`. `tenant_id` matches the newly-created
tenant.

Recovery: re-run W4. Manual creation requires the
`User.specialist_profile` OneToOne, which is auto-created via
post_save signal — direct INSERT bypasses the signal and breaks
later steps.

### Step 5 — Service catalog minimum (1 service)

Pilot requires at least one bookable service before the provider
appears in catalog recommendations.

```sql
SELECT id, name, price, duration_minutes, is_active, category_id
FROM services_service
WHERE specialist_id = '<specialist_profile_id>'
  AND is_active = true;
```

Expected: ≥ 1 row. Recovery: provider self-serves via Ayla Pro mobile
once available; pre-mobile, ops creates via Django admin.

### Step 6 — Schedule / working hours

```sql
SELECT day_of_week, start_time, end_time, is_active
FROM appointments_specialistworkinghours
WHERE specialist_id = '<specialist_profile_id>'
ORDER BY day_of_week;
```

Expected: ≥ 1 active day. Without working hours,
`AvailabilityQueryService` returns zero slots and the customer cannot
book.

Recovery: provider self-serves via Ayla Pro mobile; pre-mobile, ops
seeds via Django admin or shell:

```python
from appointments.models import SpecialistWorkingHours
from users.models import SpecialistProfile
sp = SpecialistProfile.objects.get(id='<id>')
for dow in (1, 2, 3, 4, 5):  # Mon-Fri
    SpecialistWorkingHours.objects.create(
        specialist=sp, day_of_week=dow,
        start_time='10:00', end_time='20:00',
    )
```

### Step 7 — MAX bot / token mapping

**Bot-platform domain.** Ayla side only confirms the provider's
`bot:<max_user_id>` username resolves to a real bot interaction. Ops
verify with bot-platform team that the provider's MAX bot token is
mapped to their tenant.

### Step 8 — Booking flow E2E

Create one test booking against the new tenant + specialist, run the
catalog recommendations endpoint, verify the new master appears.

```bash
# Eligible-pool sanity (bot service token required):
curl -fsS -X POST https://<vps>/api/v1/internal/me/catalog/recommendations/ \
  -H "Authorization: Bearer $AYLA_INTERNAL_API_TOKEN" \
  -H "X-External-User-ID: bot:<test_customer_id>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Expect the new specialist to appear in `layer_2_ayla_picks` (or
`layer_3_explore` category counts).

### Step 9 — Master solo dashboard

Bot-platform / Ayla Pro mobile concern. Smoke: provider sees their
own profile + services + schedule in their app. No Ayla djangoproject
queries needed.

### Step 10 — Customer reminder flow

Trigger a test booking ≥ 1 hour ahead → wait for the T-15min
reminder push.

```sql
-- Verify booking landed with correct tenant_id
SELECT id, client_id, specialist_id, tenant_id, status, start_datetime
FROM appointments_appointment
WHERE specialist_id = '<specialist_profile_id>'
ORDER BY created_at DESC LIMIT 3;

-- Verify reminder outbox event was emitted
SELECT topic, payload->>'event_name', created_at
FROM appointments_outboxevent
WHERE payload->>'booking_id' = '<booking_id>'
ORDER BY created_at;
```

Expected: `booking.created` row + reminder dispatch entry visible in
bot-platform notification log.

## First-10-onboardings ops checklist

For the pilot's first 10 self-employed providers, run the full
10-step verification per provider. Track in this format:

| # | Provider | Tenant slug | User id | Step 1 | Step 2 | Step 3 | Step 4 | Step 5 | Step 6 | Step 7 | Step 8 | Step 9 | Step 10 | Notes |
|---|----------|-------------|---------|--------|--------|--------|--------|--------|--------|--------|--------|--------|---------|-------|
| 1 | Ольга    | olga-penza  |         |        |        |        |        |        |        |        |        |        |         |       |

After 10 clean runs (any gaps closed via FOLLOW_UP), drop the
verification to a sample check (every 10th provider) — pilot
graduation criterion per `project_pilot_scope_discipline`.

## Recovery — W4 cascade failed mid-transaction

If the W4 service exited mid-cascade and partial state landed:

1. Identify the partial state by running steps 1-6 queries above.
2. **DO NOT** complete the cascade manually for the customer
   (`role=customer` TUR) — that's the Variant E invisible-grant
   surface; let it trigger naturally on first booking.
3. For missing operational rows (Tenant, owner-TUR,
   SpecialistProfile, Services, WorkingHours), the bot-platform team
   re-runs the W4 service — it's idempotent and uses
   `get_or_create` keyed on stable identifiers.
4. If W4 re-run fails repeatedly, escalate via the bot-platform
   on-call channel; do NOT bypass the cascade by direct INSERT
   (breaks downstream outbox event causality).

## Common pitfalls

- **`User.specialist_profile` OneToOne auto-create signal.** Direct
  INSERT of SpecialistProfile (bypass W4) leaves the signal-tracked
  fields blank → catalog API returns the row but mobile crashes on
  rendering. Always use the W4 path.
- **`tenant_id` denormalisation.** `SpecialistProfile.tenant`,
  `Appointment.tenant`, `Payment` (via appointment) all keep a
  redundant `tenant_id`. W4 sets them consistently; manual edits
  must touch all three or the §H.3 strict-tenant pin (#246 sub-phase
  1.B) trips.
- **Mobile login.** Pilot uses MAX webview Mini App, not direct
  mobile. The provider doesn't log in to Ayla Pro via OTP for pilot
  — they interact only via the MAX bot. Direct mobile login is a
  post-pilot scope per `project_max_only_pilot`.

## References

- `apps/identity/services/solo_onboarding.py` (bot-platform W4 service —
  authoritative atomic-cascade implementation; cross-repo)
- ADR-0008 — role detection and staff model (bot-platform)
- ADR-0009 §5 — split-domain ownership rules
  (`docs/architecture/ADR-0009-split-domain.md`)
- `docs/runbooks/webhook-readiness-2026-05-26.md` — payment webhook
  audit (referenced from step 0 pre-requisites)
- `feedback_adr_0009_ownership_verification` (Alpha memory)
- `project_solo_provider_universal_ui` (founder decision)
- `project_pilot_scope_discipline` (founder pilot cuts)

_Authored by: Alpha (Claude Opus 4.7) on 2026-05-26. Task #98. PR #167._
