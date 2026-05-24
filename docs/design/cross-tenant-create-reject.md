# Cross-Tenant Create Reject — Options for Founder (FINALIZED)

**Status:** ✅ Decided. Phase 0 shipped, Phase 1 spec in PR #149.
**Author:** Stream Alpha (after closing all 9 ADR-0009 §6 enforcement layers + 18 PRs in pre-flip session 2026-05-22..23).
**Audience:** future readers researching the cross-tenant booking decision.
**Decisions log:** see §Decision log at end.

## TL;DR (post-decision)

Founder selected a **two-phase approach**:

- **Phase 0 (shipped 2026-05-24):** **Variant B (404 info hiding)** as the interim cross-tenant guard. PR #148 (commit `a8db0247`).
- **Phase 1 (pre-pilot 2026-07-15):** **Variant E (invisible relationship grant)** — semantic refinement of Variant C: instead of asking the customer to "join Casa Bella", the act of booking IS the consent gesture. Implementation plan in PR #149 (design doc `docs/design/246-tenant-user-relationship.md`). Aligns with `ayla-first-strategic-pivot` framing — AI belongs to the user, not the salon.

The original framing below (4 options A/B/C/D + Penza-only pilot assumption) is preserved as historical context. Founder explicitly rejected the "Penza = single tenant" framing — pilot has 5-10 salons (each = separate tenant), friend-of-friend share links are growth mechanic from day 1. Variant B alone breaks UX; Variant E is the production answer.

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

### E — Invisible relationship (SELECTED for Phase 1)

Surfaced during founder review of this doc. Refinement of Variant C: tenant-membership is a **backend implementation detail**, not a product concept for the customer.

> From the customer's perspective:
> - NOT "I'm joining Casa Bella"
> - INSTEAD: "I'm booking Master Tanya through Ayla"

Aligns with `ayla-first-strategic-pivot`: AI belongs to the user, the salon is a *provider*. The act of booking is the consent gesture. No "join salon" dialog, no friction.

**Mechanics (per founder ack 2026-05-24 — 5 architecture forks F1-F5):**

```python
# appointments/serializers.py — Phase 1 Variant E shape
def validate(self, attrs):
    request = self.context["request"]
    request_tenant = getattr(request, "tenant", None)
    specialist = SpecialistProfile.objects.only("tenant_id").get(
        pk=attrs["specialist_id"],
    )
    if request_tenant and specialist.tenant_id and \
       specialist.tenant_id != request_tenant.id:
        # Invisibly grant TUR(user, specialist.tenant). No 404, no
        # consent dialog. The booking attempt IS the consent.
        TenantUserRelationship.objects.get_or_create(
            user=request.user,
            tenant_id=specialist.tenant_id,
            is_active=True,
            defaults={"granted_by": "self"},
        )
    return attrs
```

| Pros | Cons |
|---|---|
| Zero customer-facing friction. Share-link referrals just work. | Requires `TenantUserRelationship` model from Sprint 1 Track A #246 — landing in this repo via the implementation PR per PR #149 plan. |
| Privacy posture: default-deny + green-zone visibility (per F5 ack). | Adversarial reviewers will challenge: invisible grant = invisible consent. Mitigation: each granted TUR row carries `granted_by='self'` + `granted_at=now()`; the user's tap on "book" is auditably the consent. |
| Aligns with `ayla-first-strategic-pivot` framing. | TUR revoke semantics need product policy (silent vs notification — Q1 of open questions). |
| Drops the Phase 0 Variant B 404 once Variant E lands. | Drops `IsTenantMember` from booking viewset (F4 ack = Y option). Defense-in-depth narrows on that endpoint; queryset filter remains primary guard. |

**Phase 1 design fully specified in `docs/design/246-tenant-user-relationship.md` (PR #149).** That doc captures the 5 architecture forks ack-ed by founder, schema choice (β partial-unique + history), JWT integration (A: primary + X-Tenant override), 5-sub-phase migration plan, test coverage matrix, and 4 remaining open product questions.

## Phased approach (FINAL — post founder ack 2026-05-24)

| Phase | Variant | Status | PR / Tracking |
|---|---|---|---|
| **Phase 0** (2026-05-24 → pilot) | **B (404 info hiding)** | ✅ Shipped | PR #148 (commit `a8db0247`) |
| **Phase 1** (pre-pilot 2026-07-15) | **E (invisible relationship)** | 📋 Plan in flight | PR #149 (`docs/design/246-tenant-user-relationship.md`); awaiting founder ack on 4 open questions |
| **Phase 2** (post-pilot M5+) | TUR ergonomics: explicit relationship management UI, "leave salon" CTA | Future | Sprint 2 epic |

### Why founder rejected the original phased plan

Original doc framed Phase 1 as "Variant B + Penza-only assumption." Founder corrected the framing:

- Pilot Penza ≠ single tenant. Pilot = 5-10 salons in Penza, each = separate tenant.
- Friend-of-friend share links = growth mechanic from **day 1**, not post-pilot.
- Variant B alone (404 "specialist not found") **breaks share-link UX**. Customer retention takes a hit at the worst possible moment (first user touchpoint).
- Variant E is the production answer — no friction, no consent dialog, semantically aligned with "AI belongs to the user."

**One-line decision:** Phase 0 = B ✅ (shipped). Phase 1 = E (implementation PR pending founder Q&A on PR #149).

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

## Decision log

| Date | Event | Outcome |
|---|---|---|
| 2026-05-22..23 | Stream Alpha closes 9 ADR-0009 §6 enforcement layers (#142, #144, #145 + sibling PRs). Cross-tenant integrity bug surfaced as out-of-scope from adversarial review. | Need a product decision on cross-tenant create UX. |
| 2026-05-23 | Stream Alpha drafts this doc with options A/B/C/D + "Penza-only" Phase 1 assumption. PR #147 opened. | Routed to founder. |
| 2026-05-24 (morning) | Founder verbal ack: pick Phase 0 = B, Phase 1 = a new Variant E (invisible relationship). Rejected the Penza-single-tenant framing. | Variant E added to this doc + 5 architecture forks (F1-F5) raised. |
| 2026-05-24 (midday) | Founder acks all 5 forks: F1=β (partial unique + history), F2=frozen + block new, F3=A (primary in JWT + X-Tenant override), F4=Y (Variant E in serializer, drop `IsTenantMember` from booking viewset), F5=default-deny + green zone. | Implementation plan written: `docs/design/246-tenant-user-relationship.md`, PR #149 opened. |
| 2026-05-24 (afternoon) | Phase 0 Variant B shipped (PR #148, commit `a8db0247`). | Integrity bug closed; pre-flip CI green. |
| 2026-05-24 (evening) | This doc finalized + canonical record stored. PR #147 mergeable. | Doc closes its purpose. |
| Pending | Founder answers 4 open product questions (TUR revoke notifications, specialist mobility, JWT primary tiebreak, downstream consumer access). | Implementation PR opens once answered. |

## References

- ADR-0009 §Hard rule #6 (`tenant_id` claim is `active_tenant_id`).
- PR #142 (#520) — `IsTenantMember` + queryset filter (replaced for booking viewset in Variant E).
- PR #144 (#568) — tenant_id stamping at create.
- PR #145 (#590) — null=False schema lock + CheckConstraint.
- PR #148 — Phase 0 Variant B (404 info-hiding interim). Merged `a8db0247`.
- PR #149 — Phase 1 implementation plan (`docs/design/246-tenant-user-relationship.md`).
- Sprint 1 Track A #246 — `TenantUserRelationship` model.
- `feedback_pr_workflow_code_reviewer` memory — severity discipline applied during the original 18-PR Track B session.
- `ayla-first-strategic-pivot` — framing rationale for Variant E.
- Personalisation engine spec — sensitivity-zoned data for default-deny privacy (F5 ack).
