# S3-CAL — External Busy / Calendar Sync Design (2026-07)

**Epic:** `AndreyDeveloper84/ai-bot-platform#1044` + Ayla `#200`.
**Branch:** `feat/1044-s3cal-external-busy` (off `origin/dev`).
**Depends on:** S3A (#205) + contract-fix (#207), both merged to dev.
**Status:** design for approval — **no code until forks are signed off**.

---

## 1. Purpose & founder-lock

Ensure Ayla slot availability reflects the pilot salon's **external** calendar so
the bot never double-books against a booking made outside Ayla.

**Variant B (founder-lock):** YClients is the busy source of record. A YClients
**webhook** (inbound-only for the pilot) pushes busy changes → Ayla stores them as
external busy intervals (`company_id`→tenant, `staff_id`→specialist). Slot
computation subtracts them; a **recheck-at-confirm** closes the webhook-lag race.

### ⚠️ Abstraction caveat (founder open question)

Variant B assumes the salon **operates in YClients**. Pilot salon `884045`'s
YClients license has expired (open question to founder). If the salon leaves
YClients, S3-CAL **pivots to Variant A** (Ayla-primary — Ayla's own bookings are
the only source). **Therefore the busy source is abstracted end-to-end:** the
slot engine consumes `ExternalBusyInterval` rows and knows nothing about YClients.
Only the *ingress* (the webhook writer) is YClients-specific and isolated. Variant
A = simply stop feeding the table from YClients (or feed it from another source);
zero change to the read/slot path. **This design hardcodes YClients nowhere except
the webhook ingress module.**

---

## 2. Seam analysis (grounded in current code)

The booking engine **already** abstracts busy sources — this is the whole design's
leverage. `appointments/infrastructure/availability/providers.py` docstring:
*"New sources (external calendars, equipment, rooms) are added as new classes,
without touching AvailabilityQueryService."*

| Path | Today | External-busy hook | Boundary |
|---|---|---|---|
| **Read** (slot display; `AvailabilityQueryService` → `make_read_provider()`) | `CompositeAvailabilityProvider([Booking, TimeOff])` | **Add a 3rd provider** → composes in, zero change to SlotBuilder / QueryService | ✅ in scope ("чтение slots-busy для CAL") |
| **Write** (confirm; `CreateBookingService._execute_atomic`) | Direct `Appointment.objects.filter(...).select_for_update()` conflict count — **bypasses the provider abstraction** (`create_booking_service.py:137`) | Needs an explicit external-busy check **inside the atomic block** | 🔴 touches appointments-write logic → **authorization-gated** |

This asymmetry is the crux of fork 3: read-path is a clean drop-in; recheck-at-
confirm requires a minimal, authorized touch to the booking write path.

---

## 3. Fork 1 — where external busy is written

New model **`services.ExternalBusyInterval`** (my domain; source-abstracted):

| field | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant` | FK `tenants.Tenant`, PROTECT | resolved from YClients `company_id` |
| `specialist` | FK `users.SpecialistProfile`, CASCADE | resolved from YClients `staff_id` |
| `start_at` / `end_at` | DateTimeField (UTC) | the busy interval |
| `source` | CharField choices `yclients` (extensible), default `yclients` | provenance — **not** hardcoded downstream |
| `external_id` | CharField(64), blank | source record id (idempotent upsert key) |
| `raw_payload` | JSONField, default dict | original webhook body (audit / replay) |
| `received_at` | DateTimeField | when Ayla ingested it (staleness signal for recheck) |
| `created_at` / `updated_at` | auto | |

- Constraint: `unique(source, external_id, tenant)` when `external_id` non-blank
  (idempotent webhook redelivery).
- Index: `(specialist, start_at, end_at)` — the slot-window query.
- Specialist resolution: `SpecialistProfile.yclients_staff_id` (already indexed,
  `db_index=True`) primary; `ExternalSourceMapping(external_type='staff')` (S3A)
  as the canonical fallback.

## 4. Fork 2 — how busy-guard reads in slot calc (READ path, in scope)

New provider implementing the existing `BusyIntervalProvider` Protocol:

```python
class ExternalBusyIntervalProvider:            # source-agnostic
    def get_busy_intervals(self, specialist_id, day_start_utc, day_end_utc):
        rows = ExternalBusyInterval.objects.filter(
            specialist_id=specialist_id,
            start_at__lt=day_end_utc, end_at__gt=day_start_utc,
        )
        return [TimeInterval(start_at=clip(...), end_at=clip(...)) for r in rows]
```

Register it in `make_read_provider()` (and `make_write_provider()` for symmetry):

```python
CompositeAvailabilityProvider([
    BookingBusyIntervalProvider(...), TimeOffBusyIntervalProvider(),
    ExternalBusyIntervalProvider(),          # <-- new
])
```

- **SlotBuilder / AvailabilityQueryService untouched.** The provider list is the
  designed extension point.
- **Placement decision (fork 2a):** the provider class reads a `services` model but
  implements an `appointments` Protocol. **Proposed:** put the class in
  `services/` (my domain) and register it in the `appointments` factory — the
  factory edit is the one-line "add a busy-source", squarely inside the allowed
  "чтение slots-busy для CAL" carve-out. *(Confirm placement.)*
- Feature-flag `EXTERNAL_BUSY_ENABLED` (default off) so registration is inert
  until the pilot webhook is live — and the Variant-A pivot flips it off.

## 5. Fork 3 — recheck-at-confirm contract (WRITE path, 🔴 authorization-gated)

Webhooks lag; the read-path table can be stale at confirm. `_execute_atomic`
currently checks only `Appointment` overlaps. Two levels:

- **Level 1 — local recheck (minimal authorized touch).** Inside the existing
  atomic block, after the Appointment conflict count, add an
  `ExternalBusyInterval` overlap check for `target_interval`; if it overlaps →
  raise the existing slot-conflict path (or a new `EXTERNAL_SLOT_TAKEN`). This is
  ~5 lines in `_execute_atomic`, reusing the transaction. **Crosses the
  appointments-write boundary → needs your explicit authorization** (S3-CUT-style).
- **Level 2 — live recheck (closes webhook-lag window).** At confirm, synchronously
  query YClients availability for the interval; on busy → reject. Requires a
  pluggable `ExternalBusyRechecker` port (Variant B = YClients client; Variant A =
  no-op). Heavier (network in the booking hot path, timeout/fallback policy).
  **Proposed:** defer to a follow-up; Level 1 + tight webhook latency is enough for
  a 1-salon pilot. *(Confirm: Level 1 now, Level 2 later? And is the booking-write
  touch authorized?)*

**Contract for the bot:** confirm may now fail with `EXTERNAL_SLOT_TAKEN` (409) —
the bot surfaces "slot just taken" and re-fetches availability. This must be added
to the booking REST error contract.

## 6. YClients webhook ingress (inbound-only, isolated)

`POST /api/v1/internal/catalog/yclients/busy-webhook/` (or a dedicated hook path):
- Auth: shared-secret / HMAC (reuse existing webhook-auth pattern — to confirm).
- Body → resolve `company_id`→tenant, `staff_id`→specialist → **upsert**
  `ExternalBusyInterval` (idempotent on `(source, external_id, tenant)`); delete/
  supersede on cancellation events.
- **The only YClients-coupled module.** Everything downstream is source-agnostic.

## 7. Chunks (per-chunk PR → dev)

- **S3-CAL.1** — `ExternalBusyInterval` model + migration + admin + model tests
  (in scope, additive).
- **S3-CAL.2** — `ExternalBusyIntervalProvider` + read-path registration +
  feature flag + slot tests (read path; in-scope carve-out).
- **S3-CAL.3** — webhook ingress endpoint + resolution + idempotent upsert + tests.
- **S3-CAL.4** — recheck-at-confirm Level 1 (**authorization-gated**; booking-write
  touch) + `EXTERNAL_SLOT_TAKEN` contract + tests.
- Level 2 live recheck — separate follow-up if founder wants webhook-lag closed.

## 8. Out of scope / guardrails

- No changes to payments/users; appointments touched **only** via the busy-source
  carve-out (read providers) and — if authorized — the Level-1 recheck.
- Outbound sync to YClients (Ayla → YClients) is **not** in Variant B pilot
  (inbound-only).
- Live end-to-end webhook test waits on pilot creds / license resolution
  (orchestrator-coordinated); does not block S3-CAL.1/.2 build.

## 9. Open decisions for sign-off
1. **Fork 2a placement** — provider class in `services/`, factory edit in
   `appointments/` (proposed) — OK?
2. **Fork 3 authorization** — is the Level-1 `_execute_atomic` touch authorized
   now, or must recheck-at-confirm avoid appointments-write entirely (needs an
   alternative design)?
3. **Fork 3 scope** — Level 1 now, Level 2 (live pull) deferred — agree?
4. **Webhook auth** — reuse which existing secret/HMAC pattern?
