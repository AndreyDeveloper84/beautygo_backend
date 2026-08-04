# Pilot pre-deployment smoke tests — Alpha territory

> **Productive-standby contribution** (task #102, `feedback_productive_standby`).
> Verifies that the 13 PRs shipped this Sprint 1 session land on
> staging/prod VPS as expected. Doc-only — no code.
>
> **When to run:** after deploy to staging VPS, T−3 weeks before pilot
> launch 2026-07-15. Re-run after each material change to any of the
> surfaces below.
>
> **Companion docs:** `solo-provider-bootstrap.md` (ops bootstrap),
> `webhook-readiness-2026-05-26.md` (YooKassa webhook audit).

## Pre-flight (zero-row state)

Before exercising scenarios, confirm bring-up:

```bash
# 1. Migrations applied — last entries should include:
#    services/0010_service_aftercare_text  (#100 / PR #168)
#    users/0013_specialistprofile_yclients_staff_id  (#92 / PR #160)
python manage.py showmigrations services users | tail -8

# 2. Beat schedule live — these task names must appear:
#    notifications.dispatch_appointment_reminders
#    notifications.dispatch_post_visit_aftercare
#    appointments.tasks.dispatch_outbox_events
celery -A djangoProject inspect scheduled | grep -E "aftercare|reminders|outbox"

# 3. Required prod env (`_REQUIRED_PROD_ENV` in djangoProject/settings/prod.py):
for v in DJANGO_SECRET_KEY GOOGLE_CLIENT_ID APPLE_CLIENT_ID \
         YOOKASSA_WEBHOOK_ALLOWED_IPS AYLA_INTERNAL_API_TOKEN; do
  printf '%-32s : %s\n' "$v" "${!v:+set}"
done

# 3a. Identity provisioning (E2E-BOT-02B) — OPTIONAL, off by default:
printf '%-32s : %s\n' AYLA_IDENTITY_PROVISIONING_TOKEN \
       "${AYLA_IDENTITY_PROVISIONING_TOKEN:+set}"
```

`AYLA_INTERNAL_API_TOKEN` is technically not in `_REQUIRED_PROD_ENV`
yet (see ai-bot-platform#868); set it manually.

`AYLA_IDENTITY_PROVISIONING_TOKEN` gates
`POST /api/v1/internal/users/bind-external/` (identity binding,
provisioning-only). Empty = endpoint disabled (fail-closed, 403 for
every caller). To provision: generate an independent secret
(`openssl rand -hex 32`), set it ONLY on the Ayla side (never deploy
to bot-platform), and keep it DISTINCT from
`AYLA_INTERNAL_API_TOKEN` — equal values are a hard misconfiguration
(system check `users.E001` fails at boot and the permission fails
closed). Production bot-driven binding is not supported until a
verified ownership flow exists; the token is for trusted
provisioning / E2E bootstrap / ops only. Pre-prod gate: binding moves
proxy-held personal data outside the per-user delete/export surface
(152-ФЗ) — enabling the token in production requires the domain
owner's decision to either extend erasure to linked proxies or refuse
binding data-holding proxies (tracked in beautygo_backend#220,
AYLA-DEC-0016 §4).

## Smoke scenarios — 13 PR coverage

### S1. #246 sub-phase 1.F — `GET /api/v1/users/me/tenant-relationships/` (PR #156)

```bash
# Authenticated client (JWT). Should list active customer TURs only.
TOKEN=<jwt>
curl -fsS -H "Authorization: Bearer $TOKEN" \
  -H "X-App-Type: client" \
  https://<vps>/api/v1/users/me/tenant-relationships/ | jq '.data.data | length'
```

Expected: 200, `.data.data` is array (empty for new user; one entry
per active CUSTOMER-role TUR for returning customer).

### S2. #716 — AI booking uses `request.tenant.id` (PR #157)

No direct endpoint smoke — verified indirectly via S7 + S9. If the
AI confirm-booking flow grants TUR against the wrong tenant, S2
manifests as a TUR row for the wrong tenant after S7's AI booking.

### S3. #85 — `POST /api/v1/payments/internal/{id}/retry/` (PR #158)

Bot service auth path. Customer's existing failed Payment retried via
bot:

```bash
# Body MUST carry client_id matching the resolved actor —
# defense-in-depth. Mismatch → 403 CLIENT_MISMATCH.
curl -fsS -X POST \
  -H "Authorization: Bearer $AYLA_INTERNAL_API_TOKEN" \
  -H "X-External-User-ID: bot:42" \
  -H "Content-Type: application/json" \
  -d "{\"client_id\":\"$BOT_42_USER_UUID\",\"return_url\":\"https://ayla.app/ok\"}" \
  https://<vps>/api/v1/payments/internal/$FAILED_PAYMENT_ID/retry/
```

Expected: 201 with new `confirmation_url`. Mismatch on client_id
→ 403 `{"error":{"code":"CLIENT_MISMATCH"}}`. See PR #158 §H.3 tests
for full coverage matrix.

### S4. #92 — `POST /api/v1/masters/internal/by-yclients-staff-ids/` (PR #160)

W1 admin batch lookup. Bearer-only, no X-External-User-ID:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $AYLA_INTERNAL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"tenant_id\":\"$TENANT_UUID\",\"yclients_staff_ids\":[\"10001\",\"10002\"]}" \
  https://<vps>/api/v1/masters/internal/by-yclients-staff-ids/
```

Expected: 200 with `{data: {masters: [...], not_found_staff_ids: [...]}}`.
Cross-tenant isolation: passing a `tenant_id` the staff don't belong
to returns all ids in `not_found_staff_ids`.

### S5. #91 — Cross-domain safety contract linter (PR #161)

Validator runs on `CrossDomainRule.clean()`. Audit-snapshot smoke:

```bash
python manage.py shell -c "
from nutrition.data.cross_domain_rules_seed import TRACK_E_RULES
from nutrition.services.cross_domain_safety import validate_safety_contract, has_errors

for seed in TRACK_E_RULES:
    class _Stub:
        insight_text_template = seed['insight_text_template']
        rationale_text = seed['rationale_text']
        disclaimer_text = seed['disclaimer_text']
        excluded_health_flags = list(seed['excluded_health_flags'])
    v = validate_safety_contract(_Stub())
    status = 'FAIL' if has_errors(v) else 'PASS'
    print(f'{status}  {seed[\"rule_id\"]}')"
```

Expected (per audit doc §7): 3 FAIL + 2 PASS — pilot baseline. No
seed rule should silently pass without curator review.

### S6. #246 Q1 — `POST /tenants/me/relationships/{id}/revoke/` (PR #162)

Tenant-admin revokes a customer:

```bash
# Tenant admin's JWT, with X-Tenant matching their admin TUR.
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "X-App-Type: pro" \
  -H "X-Tenant: $TENANT_SLUG" \
  -H "Content-Type: application/json" \
  -d '{"reason":"smoke_test","notify_customer":false}' \
  https://<vps>/api/v1/tenants/me/relationships/$TARGET_USER_ID/revoke/
```

Expected: 204 No Content. Verify in DB:

- `users_tenantuserrelationship.is_active=false`
- `appointments_outboxevent.topic='tenant.relationship.revoked'` row
  created (audit ALWAYS emitted)

### S7. #246 Q2 — Specialist-departure cascade (PR #163)

Trigger by revoking a STAFF-role TUR. The cascade should cancel all
the specialist's active future bookings + emit `booking.cancelled`
per booking:

```sql
-- Pre-revoke: count active bookings for the target specialist
SELECT COUNT(*) FROM appointments_appointment
WHERE specialist_id = '<specialist_id>'
  AND tenant_id    = '<tenant_id>'
  AND status IN ('pending','awaiting_payment','confirmed')
  AND start_datetime > now();
```

Run revoke via S6 with the staff user as target. After revoke:

```sql
-- Post-revoke: all rows should be 'cancelled' with reason
SELECT id, status, cancellation_reason
FROM appointments_appointment
WHERE specialist_id = '<specialist_id>'
  AND tenant_id    = '<tenant_id>'
  AND status = 'cancelled'
  AND cancellation_reason = 'specialist_departure';
```

Customer push: each cancelled booking generates an
`appointment_cancelled_specialist_departure` Notification —
"Запись отменена. Салон обещает связаться."

### S8. #97 — Records endpoints (PR #164)

W1 customer-records-flow. All three endpoints under
`/api/v1/internal/me/bookings/`:

```bash
# List — upcoming
curl -fsS -X GET \
  -H "Authorization: Bearer $AYLA_INTERNAL_API_TOKEN" \
  -H "X-External-User-ID: bot:42" \
  "https://<vps>/api/v1/internal/me/bookings/?section=upcoming"

# Detail
curl -fsS \
  -H "Authorization: Bearer $AYLA_INTERNAL_API_TOKEN" \
  -H "X-External-User-ID: bot:42" \
  https://<vps>/api/v1/internal/me/bookings/$BOOKING_ID/

# Repeat-intent
curl -fsS -X POST \
  -H "Authorization: Bearer $AYLA_INTERNAL_API_TOKEN" \
  -H "X-External-User-ID: bot:42" \
  https://<vps>/api/v1/internal/me/bookings/$BOOKING_ID/repeat-intent/
```

Expected: each item carries `derived_status` from the 17-value
taxonomy. Multi-tenant: items from ALL of the customer's tenants
appear in one response.

### S9. #99 — `/catalog/recommendations` (PR #165)

W1 booking flow Phase B:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $AYLA_INTERNAL_API_TOKEN" \
  -H "X-External-User-ID: bot:42" \
  -H "Content-Type: application/json" \
  -d '{"lat":55.75,"lon":37.62,"goal":"маникюр"}' \
  https://<vps>/api/v1/internal/me/catalog/recommendations/
```

Expected: 3-layer response. `layer_2_ayla_picks` has ≤ 3 items each
with a `reasoning_text` string. `layer_3_explore.categories` has ≤ 10
entries. **Layer 1 must NOT be hidden by `goal`** — see PR #165 test
`test_layer_1_not_filtered_by_goal` for the invariant.

### S10. #101 — YooKassa webhook (PR #166)

Per the `webhook-readiness-2026-05-26.md` runbook — manual curl
sequence verifies IP allowlist + Basic Auth + idempotency layers.
This smoke is REDUNDANT with the dedicated runbook; re-running the
existing procedure suffices.

### S11. #98 — Solo provider bootstrap (PR #167)

10-step verification per `solo-provider-bootstrap.md`. Run that
runbook fully for the first staging-onboarded provider.

### S12. #100 — B9 aftercare beat (PR #168)

```bash
# 1. Seed an approved aftercare_text on a Service via admin or shell:
python manage.py shell -c "
from services.models import Service
svc = Service.objects.get(id='<svc_id>')
svc.aftercare_text = 'Не мочить кутикулу 2 часа. Питательный крем 2 раза в день.'
svc.save()
"

# 2. Mark a recent completed booking's end_datetime to land in the
#    [now-2h30m, now-2h] window. (Production: wait for a real
#    appointment to age into the window.)
python manage.py shell -c "
from appointments.models import Appointment
from django.utils import timezone
from datetime import timedelta
a = Appointment.objects.get(id='<booking_id>')
end = timezone.now() - timedelta(hours=2, minutes=15)
a.end_datetime = end
a.start_datetime = end - timedelta(hours=1)
a.status = 'completed'
a.save()
"

# 3. Trigger the beat manually OR wait < 5 min.
python manage.py shell -c "
from notifications.tasks import dispatch_post_visit_aftercare
print(dispatch_post_visit_aftercare())
"

# 4. Verify Notification row created with template_id='post_visit_aftercare'.
```

Expected: `dispatch_post_visit_aftercare()` returns `{queued: 1, ...}`
on first call, `{queued: 0, skipped: 1, ...}` on second (idempotent).
Without `aftercare_text` filled: `{queued: 0, ...}` always (safety
default — no LLM, no push).

## Backward-compat verification

The 13 PRs introduced new endpoints + one field on Service +
several new outbox topics. Existing endpoints MUST continue to
work. Quick regression smoke:

| Endpoint | Smoke |
|---|---|
| `POST /api/v1/auth/verify-otp/` | Verify mobile OTP login path still mints JWT |
| `GET /api/v1/specialists/` | Catalog browse unchanged |
| `POST /api/v1/appointments/` | Mobile-direct booking unchanged |
| `POST /api/v1/payments/create/` | Mobile-direct payment unchanged |
| `POST /api/v1/payments/{id}/retry/` | Mobile-direct retry (JWT) unchanged after #85 refactor |
| `POST /api/v1/payments/{id}/refund/` | Refund path unchanged |
| `POST /api/v1/payments/webhook/` | YooKassa webhook ack unchanged |
| `GET /api/v1/users/me/` | Profile read unchanged |
| `POST /api/v1/nutrition/internal/scan/` | Bot food-scan path unchanged |

Smoke procedure: pre-pilot rehearsal of the mobile booking flow with
a test customer + test specialist. If everything in the catalog +
booking + payment chain round-trips, backward-compat holds.

## Known limitations / things to watch

- **`MULTI_TENANT_STRICT` flip** scheduled 2026-05-28 (per
  `users/middleware.py` docstring). Once flipped, missing X-Tenant
  on tenant-scoped endpoints → 400. Smoke any pilot endpoint that
  expects implicit tenant before the flip.
- **TUR seed rules audit** (S5): 3 of 5 cross-domain rules currently
  FAIL the safety contract by design. Curator must rewrite the
  failing strings before activation. Do NOT flip `is_active=True`
  on a failing rule — `CrossDomainRule.clean()` blocks it.
- **B9 aftercare** stays silent until ops fills `Service.aftercare_text`
  per service. Default empty string is the safety state.

## Cross-doc references

- `docs/runbooks/solo-provider-bootstrap.md` — onboarding ops
- `docs/runbooks/webhook-readiness-2026-05-26.md` — webhook audit
- `docs/safety/cross_domain_safety_contract.md` — safety contract
- `docs/design/246-tenant-user-relationship.md` — #246 design doc
- ADR-0009 split-domain (`docs/architecture/ADR-0009-split-domain.md`)

## FOLLOW_UPs tracker

The 13 PRs filed 38 follow-up issues on `ai-bot-platform`. Pre-pilot
triage should walk the PRE_PILOT-tagged ones (full list in PR
descriptions). Post-pilot tagged items can wait for the T+7d retro.

_Authored by: Alpha (Claude Opus 4.7), 2026-05-27._
