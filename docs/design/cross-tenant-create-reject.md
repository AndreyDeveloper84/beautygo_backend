# Cross-Tenant Create Reject — Options for Founder

**Status:** design doc, decision pending.
**Author:** Stream Alpha (after closing all 9 ADR-0009 §6 enforcement layers + 18 PRs in pre-flip session 2026-05-22..23).
**Audience:** founder (product decision) + Stream Alpha (implementation).
**Filing:** ai-bot-platform (TBD ticket).

## TL;DR

A customer authenticated in tenant A who tries to create an appointment with a specialist who lives in tenant B currently gets **403 FORBIDDEN** from `IsTenantMember` (PR #142, #520). This works but offers no growth path — pre-pilot it's fine, post-pilot it kills friend-of-friend referrals.

This doc lays out 4 stances + recommends a **phased move** rather than a one-shot fix.

## Current behaviour (Phase 0)

1. Customer Anna (`user.tenant=A`) hits `POST /api/v1/appointments/` with `X-Tenant: A` and `specialist_id = olga` where Olga is `specialist.tenant=B`.
2. `IsTenantMember` accepts (A == A on the header check).
3. `AppointmentViewSet.create` calls `CreateBookingService._execute_atomic` which stamps `tenant_id = specialist.tenant_id = B` on the new row.
4. Subsequent reads via the now-strict queryset filter `qs.filter(tenant=request.tenant)` won't return the row (tenant=B excluded when X-Tenant=A).
5. Customer sees the booking land then disappear — confusing UX, mismatched billing context, B-tenant gets an Anna booking the A-tenant payment processor doesn't know how to handle.

**There's an actual cross-tenant integrity bug here even with 403** — the **create succeeds** at the row level but the booking is unreachable to the actor. Reported by adversarial Code Reviewer on #142 as out-of-scope but flagged for this decision.

## Options

### A — Reject at create-time with 403 + actionable error

`AppointmentCreateSerializer.validate_specialist_id` checks `specialist.tenant_id != request.tenant.id` → raise `ValidationError("Specialist belongs to a different tenant; switch tenant or use a cross-tenant invitation link.")`.

| Pros | Cons |
|---|---|
| Cheapest fix. ~30 min code change. | Leaks tenant existence ("there IS a specialist Olga, but in another tenant"). |
| Symmetric with view-level `IsTenantMember` 403. | UX dead-end — no path forward for the customer. |
| No data integrity risk — booking never lands. | Kills friend-of-friend share-link conversion. |

### B — Reject at create-time with 404 (info hiding)

Same validator, but raise `NotFound("Specialist not found.")`. Mirrors the cross-specialist 404 pattern Stream Alpha already uses in `complete()` / `no_show()` (don't leak existence cross-specialist; same principle cross-tenant).

| Pros | Cons |
|---|---|
| No tenant existence leak. | Same UX dead-end as A. |
| Consistent with existing 404 patterns. | Hides a real product opportunity (cross-tenant referrals). |
| ~30 min code change. | Customer thinks the share link is broken. |

### C — Multi-tenant customer (TenantUserRelationship model)

Customer belongs to N tenants via a many-to-many table. When Anna tries to book Olga in tenant B, the create endpoint detects "user doesn't yet have a relationship with tenant B" → returns `409 NEEDS_TENANT_CONSENT` with the tenant B name + a consent CTA. Customer taps "Join Casa Bella to book" → `POST /api/v1/tenants/{slug}/join/` → `TenantUserRelationship.objects.create(user=anna, tenant=B, is_active=True)` → retry the booking.

| Pros | Cons |
|---|---|
| Unlocks growth — friend-of-friend referrals work. | **Blocked on Sprint 1 Track A #246** — `TenantUserRelationship` model doesn't exist in this repo yet. |
| Business-model-correct for marketplace platform. | Big consent / privacy design — what data does tenant B see about Anna? |
| Aligns with ADR-0009 §6 (`tenant_id` claim is `active_tenant_id`, not permanent). | Mobile flow rewrite (tenant context switch). |
| Reusable for specialists who move salon (tenant A → B). | 10-15 day implementation. |

### D — Redirect to onboarding in tenant B

When Anna lands on Olga's share link, the backend returns `307 TEMPORARY_REDIRECT` with `Location: /onboard?tenant=B&specialist=olga`. Mobile app re-onboards Anna into tenant B's context.

| Pros | Cons |
|---|---|
| Natural growth lever — share links just work. | If Anna is already onboarded in A, this CREATES a tenant-B Anna duplicate — split-customer problem. |
| No new model required. | Identity confusion: are A-Anna and B-Anna the same person? Privacy, GDPR. |
| Customers don't manage relationships. | Per-tenant booking history fragmentation. |

## Recommended phased approach

| Phase | Default | Why |
|---|---|---|
| **Phase 0 (now → 2026-05-28)** | **B (404 info hiding)** | The current 403 path has a real integrity bug (booking lands but unreachable). Switching to a serializer-level 404 closes the integrity hole, costs ~30 min, matches existing 404 patterns. No growth funnel to protect yet at pilot scale. |
| **Phase 1 (M4 pilot 2026-07-15)** | **B (404)** + ops-level "Penza-only" assumption | Pilot is Penza-scoped per PRD. Cross-city referrals not in scope. Keep B. File growth as Sprint 2 epic. |
| **Phase 2 (post-pilot growth)** | **C (multi-tenant)** | After Sprint 1 Track A #246 lands `TenantUserRelationship`, build the consent flow + retry semantics. Real growth lever once data justifies it. |
| **Phase 3 (later)** | Consider D for marketing campaigns specifically (one-tap onboarding via QR code) — but the C model already covers most growth cases. |

**One-line recommendation:** Ship Option B now (1 PR, ~30 min), file C as Sprint 2 epic.

## Implementation impact per option

| Option | Files changed | Effort | New tests |
|---|---|---|---|
| Stay 403 | none | 0 — but leaves the integrity bug | none |
| **A — 403 actionable** | `appointments/serializers.py` (`validate_specialist_id`) | ~30 min | 1 — pin the validation message |
| **B — 404 info hiding** | `appointments/serializers.py` (same validator, different exception) | ~30 min | 1 — pin 404 + assert no row created |
| C — multi-tenant | `users/models.py` (new TenantUserRelationship), `users/permissions.py` (rewrite IsTenantMember), `appointments/views.py`, mobile flow, consent UI, privacy policy update | 10-15 days | ~20 |
| D — redirect | `appointments/views.py`, mobile app, share-link spec, identity-merge backend | 5-8 days | ~10 |

## Integrity-bug fix details (B-recommended path)

```python
# appointments/serializers.py
class AppointmentCreateSerializer(serializers.Serializer):
    ...
    def validate(self, attrs):
        specialist = SpecialistProfile.objects.get(pk=attrs["specialist_id"])
        request = self.context["request"]
        request_tenant = getattr(request, "tenant", None)
        if (
            request_tenant is not None
            and specialist.tenant_id is not None
            and specialist.tenant_id != request_tenant.id
        ):
            raise NotFound("Specialist not found.")
        return attrs
```

Symmetric with `complete()` / `no_show()` 404 cross-specialist pattern in `appointments/views.py:285-301`. No row leaks, no booking lands in the wrong tenant, customer sees a clean "not found" — same UX as searching for a deleted specialist.

## Open questions for founder

1. **Pilot growth model.** Penza-only. Do we expect cross-city / friend-of-friend referrals at pilot scale, or is the "Penza wedding party" cohort all in one tenant by definition?
2. **Multi-tenant customers (Option C).** Business model: does the platform encourage customers to discover specialists across tenants, or does each tenant own its customer relationship?
3. **Privacy model for C.** When Anna "joins" tenant B, what data does B see — profile fields? booking history with tenants she's already joined? personal context (`UserPersonalContext` — `home_district`, `budget`, `favorite_masters`)? Sensitivity-zoned per the personalisation engine spec.
4. **Identity.** One customer = one User row across all tenants, or one per tenant? Phone uniqueness across tenants?
5. **Specialist mobility.** A master moves salon (tenant A → B). What happens to their:
   - Pending bookings in A — auto-cancel / transfer / keep?
   - Historical bookings — visible to B's salon owner via reports?
   - Reviews — carry to B's profile or stay at A?

## Severity classification (per `feedback_pr_workflow_code_reviewer`)

- **Current 403 integrity bug**: not category 1-7 today (no exploit, just stranded data). MUST_FIX_PRE_PILOT — by 2026-07-15.
- **Decision deadline**: founder review by Phase 1 sprint planning (~Sprint 2 epic kickoff).

## References

- ADR-0009 §Hard rule #6 (`tenant_id` claim is `active_tenant_id`).
- PR #142 (#520) — `IsTenantMember` + queryset filter (current 403 path).
- PR #144 (#568) — tenant_id stamping at create.
- PR #145 (#590) — null=False schema lock.
- Sprint 1 Track A #246 — `TenantUserRelationship` model (gates Option C).
- Personalisation engine spec — sensitivity-zoned data for Option C privacy design.
