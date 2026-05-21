# Booking source dual-mode

> **Source of truth** for whether a provider's bookings live in
> YClients or Ayla's own database. Decided at the per-`SpecialistProfile`
> level; one field, one row. Phase 0 #439.

## The two modes

| `booking_source` | System of record | Slot lookup | Booking create | Local mirror |
|---|---|---|---|---|
| `ayla_local` (default) | Ayla djangoproject | `SpecialistWorkingHours` + `SpecialistTimeOff` in this DB | Direct `Appointment.objects.create` inside the booking transaction | N/A — this *is* the truth |
| `yclients` | YClients SaaS | YClients API (`GET /book_times/…`) | YClients API (`POST /book_record/…`); `Appointment` mirrors the result | `Appointment` row stays around for the state machine + the outbox dispatcher; YClients remains canonical |

`ayla_local` is the current implicit behavior for every row before #439
shipped. Every existing specialist is backfilled to `ayla_local` by
migration `users/0011_specialistprofile_booking_source.py`.

## Schema (Phase 0 / MVP)

```python
class SpecialistProfile(models.Model):
    # …
    class BookingSource(models.TextChoices):
        AYLA_LOCAL = "ayla_local", "Ayla local DB SoR"
        YCLIENTS = "yclients", "YClients SoR"

    booking_source = models.CharField(
        max_length=20,
        choices=BookingSource.choices,
        default=BookingSource.AYLA_LOCAL,
    )
    yclients_company_id = models.CharField(
        max_length=64, blank=True, default="",
    )
```

## Booking flow branching (placeholder — not implemented in Phase 0)

The booking engine in `appointments/application/services/` will read
`specialist.booking_source` near the top of `CreateBookingService`,
`AvailabilityQueryService`, `CancelBookingService`, and
`RescheduleBookingService`, and dispatch to either:

- the existing local path (`ayla_local`) — unchanged from today.
- a YClients adapter (`yclients`) — **not built in Phase 0**; lands when
  the first YClients-using provider onboards. Adapter location is
  expected to be `appointments/infrastructure/external/yclients_*.py`.

For Phase 0 we add the field and the choices only. Adding the runtime
branch without a real YClients account to test against would be
speculative; ADR-0009 §"no half-finished implementations" rule keeps
the field declarative for now.

## Why on `SpecialistProfile` and not `Master` / `Salon` / `Tenant`

The issue body originally drafted "add to Master / Salon / Tenant" —
triplicate sources of truth. Tech-lead review on 2026-05-20 reduced
this to one field on `ProviderProfile` (the abstract name in
bot-platform Track A). In Ayla djangoproject the equivalent is
`SpecialistProfile`. One row per provider keeps the contract simple.

## Multi-location: NOT a Phase 0 problem

When (if) a provider runs more than one physical salon and the salons
use different booking systems, the field moves from `SpecialistProfile`
to a new `ProviderLocation` table. Until then a single specialist =
single source. The model carries a `TODO(phase-1.5)` comment pointing
here.

## Tests

`users/tests/test_booking_source_field_439.py` pins:

- The two choices `('ayla_local', 'yclients')`.
- The default `ayla_local` on new rows.
- `yclients_company_id` blank by default.
- Round-trip set+save+refetch for the YClients branch.

The booking-engine branching itself (when it lands) needs its own
integration test against a YClients sandbox — out of scope for #439.

## References

- ADR-0009 §Booking SoR rule (`ai-bot-platform/docs/adr/ADR-0009-ayla-split-domain-architecture.md`).
- Phase 0 sprint plan §Bucket 12 (`ai-bot-platform/docs/plans/2026-05-20-phase-0-sprint-plan.md`).
- Issue #439 (this ticket).
