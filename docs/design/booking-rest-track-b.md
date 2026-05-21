# Booking REST audit + Track B gap-fill — design doc

> **Status:** Draft 2026-05-21. Awaiting tech-lead review before any code.
> **Author:** Stream Alpha (Ayla djangoproject).
> **Scope:** Inventory the existing booking REST surface in `appointments/` and propose targeted gap-fills for Sprint 1 Track B (mobile API). Does NOT propose a from-scratch scaffold — the booking lifecycle is already substantially built.
> **Companion docs:** ADR-0009 §Mobile API split + §Domain ownership matrix, Phase 0 sprint plan §Bucket 5.

## TL;DR

What I expected to write: "scaffold POST/GET/PATCH/DELETE for booking lifecycle."

What I found: **the scaffold already exists.** The booking lifecycle is DDD-modelled (Appointment + BookingStateMachine + 4 application services + 7 REST endpoints + post-#486 envelope-compliant OutboxEvent emits). The 6-state state machine matches the ADR-0009 event taxonomy 1:1.

**Recommended Track B work** is not a scaffold but four targeted gap-fills, listed in priority order at §"Recommended gap-fills."

## Current state — what's already shipped

### Endpoints (`appointments/views.py::AppointmentViewSet`)

| Method | Path | View | Notes |
|---|---|---|---|
| GET | `/api/v1/appointments/` | `list` | Client / specialist scoped via `get_queryset`. |
| POST | `/api/v1/appointments/` | `create` | Calls `CreateBookingService`. Emits `booking.created` outbox event. |
| GET | `/api/v1/appointments/{id}/` | `retrieve` | Detail serializer. |
| POST | `/api/v1/appointments/{id}/cancel/` | `cancel` | Calls `CancelBookingService`. Refund-policy hook. Emits `booking.cancelled`. |
| POST | `/api/v1/appointments/{id}/reschedule/` | `reschedule` | Calls `RescheduleBookingService`. Emits `booking.rescheduled`. |
| POST | `/api/v1/appointments/{id}/complete/` | `complete` | Specialist-only. Marks confirmed → completed. |
| PATCH | `/api/v1/appointments/{id}/status/` | `update_status` | Spec-compliant shim that delegates to `cancel`/`complete` by inspecting `status` body. |

`urls.py` routes through `DefaultRouter` registered under `/api/v1/appointments/`. Auth via `IsAuthenticated` (rest_framework_simplejwt).

### State machine (`appointments/domain/value_objects.py`)

```
pending -> awaiting_payment -> confirmed -> completed
                            |-> cancelled
confirmed -> cancelled
confirmed -> no_show
{completed, cancelled, no_show} -> terminal
```

Transitions enforced by `BookingStateMachine.transition()`. `ACTIVE_BOOKING_STATUSES` (PENDING/AWAITING_PAYMENT/CONFIRMED) is the slot-holding set used by the conflict-check query in `CreateBookingService` and `RescheduleBookingService`.

### Application services (`appointments/application/services/`)

- `CreateBookingService` — slot-availability check + payment row creation + outbox emit, all in `@transaction.atomic`. Idempotency via `idempotency_key` on `Appointment`.
- `CancelBookingService` — state transition + refund-policy + outbox emit. `initiator_role` ∈ {client, specialist, system} drives the envelope `actor` mapping (post-#486).
- `RescheduleBookingService` — conflict-check + state-preserving move + outbox emit (with `old_start_at` for cache invalidation).
- `AvailabilityQueryService` — read-side, returns free slots for a given specialist+date.

### Outbox emits (post-#486)

All 5 booking/payment emit sites go through `appointments.infrastructure.outbox.emit_outbox_event(...)`, which wraps domain data in the ADR-0009 envelope (`event_id == OutboxEvent.id`, `event_version` from registry, `actor` validated against `{system, user, admin}`).

Live topics in `OutboxEvent.Topic`:
`booking.created`, `booking.confirmed`, `booking.cancelled`, `booking.rescheduled`, `booking.completed`, `booking.no_show`, `payment.confirmed`, `payment.refunded`, `cache.invalidate_slots`.

### Tests

- `appointments/tests/test_services.py` — service-layer unit tests for the 3 booking services.
- `appointments/tests/test_outbox_end_to_end.py` (#425) — booking-create → outbox row → dispatcher → processed.
- `appointments/tests/test_payment_app_move_426.py` (#426) — Payment-side coupling.
- `tests/contracts/test_outbox_envelope.py` (#486) — envelope shape.

## Gap analysis vs ADR-0009 §Mobile API split

ADR line 74 specifies the mobile-facing booking surface:

> Booking (create/list/get/cancel/reschedule)

All 5 are present, plus `complete` and `update_status`. **Mobile-facing endpoint coverage is 100%** for the ADR.

Gaps surface elsewhere:

| Gap | Severity | Notes |
|---|---|---|
| **No `no_show` REST endpoint** | M | `BookingStatus.NO_SHOW` + `BOOKING_NO_SHOW` topic exist, but no view marks an appointment as `no_show`. Specialist UX path is broken — they have to mark `cancelled` instead, losing the no-show signal. |
| **`complete` doesn't go through `emit_outbox_event`** | M | `appointment.complete()` (model method) probably mutates state directly; need to confirm it emits a `booking.completed` envelope. Post-#486 every emit site should use the helper for consistency. |
| **`confirmed` transition has no explicit emit site** | M | The state moves pending → awaiting_payment → confirmed when payment lands; `payments/views.py` webhook handler may or may not emit `booking.confirmed`. Need to audit. |
| **No idempotency on cancel/reschedule** | L | `create` uses `idempotency_key`; cancel/reschedule rely on the natural-no-op of "already cancelled / already at this slot" instead of an explicit key. Mobile retries on flaky network may emit duplicate outbox events. |
| **URL terminology drift** | L | ADR uses "Booking"; codebase uses "Appointment". Same concept, different label. Rename would break mobile; recommended: keep as-is, add a one-line note in the API doc explaining the mapping. |
| **Auth boundary not verified against ADR §Hard rule #6** | L | `tenant_id` claim in JWT is required to be `active_tenant_id`, not permanent. The view filters by `request.user` (and tenant via DRF-242.x middleware) but I have not traced whether `TenantUserRelationship(user_id, tenant_id)` is checked on every booking action per Hard rule #6. |
| **No `GET /api/v1/appointments/{id}/payments/`** | L | Mobile sometimes needs to see payment history for an appointment without a separate /payments call. Out of ADR scope; nice-to-have. |

## Recommended gap-fills (priority order)

### 1. `no_show` REST endpoint + emit (M, ~3-5h)

New `POST /api/v1/appointments/{id}/no-show/` (specialist-only, transition `confirmed -> no_show`). Call `emit_outbox_event(topic="booking.no_show", ...)`. Add a `MarkNoShowService` mirror of the other booking services. Tests pin the transition + outbox emit.

### 2. Audit + fix `complete` + `confirmed` emit sites (M, ~3-5h)

- Confirm `appointment.complete()` either emits `booking.completed` via `emit_outbox_event` or grow the helper call alongside. Same for the payment-webhook path that transitions to `confirmed`.
- Trace `confirmed` transition: does any code path emit `booking.confirmed`? If not, add at the point payment captures.
- Regression test: state machine transition + matching outbox topic per transition.

### 3. Verify ADR §Hard rule #6 tenant verification (L, ~2-3h)

Trace the multi-tenant middleware on each booking endpoint. Pin via a regression test that an inactive `TenantUserRelationship` causes 403 on `cancel`/`reschedule`/`complete`. This is a security boundary — Code Reviewer will want to see it.

### 4. Idempotency on cancel/reschedule (L, ~3-4h)

Add `X-Idempotency-Key` header support to both endpoints. Store in a small `IdempotencyKey` table or reuse the existing pattern. Low priority because the natural-no-op currently catches duplicates, but mobile-side retries are explicit ADR-0009 expectation.

## Out of scope (separate later work)

- **AI-initiated booking actor threading** — `ai/application/services/action_service.py` invokes `CreateBookingService()`; envelope currently tags `actor='user'`. Threading `actor` through `CreateBookingDTO` is a separate ticket (already noted in #486 PR body).
- **YooKassa hold→capture→refund scaffold** — payment lifecycle is already wired in `payments/views.py` + `payments/services.py`. Bucket 6 (#427/#428) handles bot-platform's side; Alpha's side is done.
- **Mobile API gateway routing** — Gamma owns the Nginx config (#434, Sync 5).
- **Event-contract.md documentation** — Beta owns (#441). #486 already implements the envelope; the human-readable spec is Beta's.

## Open questions for tech-lead

1. **Terminology rename** — keep `/api/v1/appointments/` or migrate to `/api/v1/bookings/`? My recommendation: **keep**. Rename = mobile-client break, no business value, lots of test-file churn. Document the mapping in the API doc and move on.
2. **`no_show` endpoint URL shape** — `/api/v1/appointments/{id}/no-show/` (hyphen) vs `/api/v1/appointments/{id}/no_show/` (underscore) vs `/api/v1/appointments/{id}/status/` (delegate via PATCH like `update_status` does for cancel/complete)? The last option is the most spec-aligned but adds branching to `update_status`.
3. **Idempotency key storage** — reuse `Appointment.idempotency_key` field for cancel/reschedule too, or introduce a separate `IdempotencyKey` table keyed on `(user_id, method, path, key)`? The latter generalises and is the bot-platform pattern.
4. **Priority gating** — does this work fit Phase 0 freeze, or is it explicit "Sprint 1 Track B prep" (outside freeze but pre-pilot)? My read: Track B prep is freeze-allowed per the sprint plan §"Allowed during freeze: bug fixes, infra migration, rebrand, event contract code, ADR/sprint docs, Sprint 1 EPICs (Track A)" — but "Track A" is explicit; Track B is grey. Need explicit OK before any code lands.
5. **Codex second opinion** — these are not high-risk changes individually, but the cumulative blast radius across 4 endpoints + the state machine is real. Worth a `/codex review` pass?

## Anti-touch note

This doc lives in `docs/design/` — a new subdirectory under `docs/`. Per the runbook anti-touch list, `docs/` is technically Beta territory. Earlier PRs (#424 `docs/setup/postgres-migration.md`, #439 `docs/architecture/booking-source-dual-mode.md`) used the same "new subdir under docs/" pattern with a tech-lead flag in the PR body. If Beta should own design docs too, happy to hand off.

## References

- ADR-0009 §Mobile API split (lines 64-87), §Domain ownership matrix (lines 34-62), §Hard rules (lines 181-189).
- Phase 0 sprint plan §Bucket 5 (Payment refactor — adjacent context).
- Issues #486 (envelope contract, just merged) + #492 (Payment table rename, just merged).
- Existing tests: `appointments/tests/test_services.py`, `tests/contracts/test_outbox_envelope.py`.
