# #246 — TenantUserRelationship Model + Sprint 1 Track A integration

**Status:** design doc, implementation pending founder ack on Phase 0 + this plan.
**Author:** Stream Alpha (after closing 9 layers of ADR-0009 §6 + Phase 0 cross-tenant 404).
**Audience:** Stream Alpha (implementation) + founder (architectural ack) + Gamma (downstream impact).
**Issue:** ai-bot-platform Sprint 1 Track A #246.
**Related PRs:** PR #142 (#520), #144 (#568), #145 (#590), PR #148 (Phase 0 cross-tenant 404).

## TL;DR

Replace `User.tenant` single FK with a many-to-many `TenantUserRelationship` table. Implement schema variant **β** (partial unique + history), deactivation **frozen** (existing bookings kept, new blocked), JWT integration **A** (primary tenant + X-Tenant header override), Variant E timing **Y** (drop `IsTenantMember` from booking endpoints, move membership check to serializer), privacy **default-deny + green zone**.

All five forks acked by founder 2026-05-24.

## Why now

Phase 0 fix (PR #148 — cross-tenant 404 info-hiding) closes the immediate integrity bug but breaks the pilot growth funnel: friend-of-friend share links from one Penza salon to another return "Specialist not found." Pre-pilot is the right window to land #246 + Variant E so by 2026-07-15 launch the funnel works.

## Schema (Variant β — partial unique + history)

```python
# users/models.py — new model

class TenantUserRelationship(models.Model):
    """A user's relationship with a tenant.

    Each (user, tenant) pair can have AT MOST ONE active row at a
    time (partial unique constraint). Inactive rows accumulate as
    history — every grant + revoke is a separate row with
    granted_at / revoked_at timestamps.

    Why history rows instead of mutating `is_active`: 152-ФЗ
    requires logging access-grant + revoke events. With history
    rows, "when did Tenant B have access to Anna's data?" is a
    queryable fact (list of (granted_at, revoked_at) pairs), not a
    last-write-wins boolean.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_relationships",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="user_relationships",
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    # Audit trail: who triggered the grant / revoke.
    # 'self' = user-initiated (e.g. Variant E invisible grant).
    # 'admin' = tenant admin (e.g. revoke after dispute).
    # 'system' = platform-initiated (e.g. tenant deletion cascade).
    granted_by = models.CharField(
        max_length=32, default="self",
        choices=[("self", "Self"), ("admin", "Admin"), ("system", "System")],
    )
    revoke_reason = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        constraints = [
            # Partial unique: at most ONE active row per (user, tenant).
            # Inactive rows can pile up as history.
            models.UniqueConstraint(
                fields=["user", "tenant"],
                condition=models.Q(is_active=True),
                name="tur_unique_active",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "tenant"],
                name="tur_lookup_idx",
            ),
            models.Index(
                fields=["user", "is_active"],
                name="tur_user_active_idx",
            ),
        ]
```

### Why partial unique vs soft-delete `is_active` flip

Founder F1 ack: β. Comparison:

| Concern | α soft-delete | β partial unique + history |
|---|---|---|
| 152-ФЗ access log | Last state only | Full grant/revoke history per pair |
| Re-grant after revoke | UPDATE row | INSERT new row |
| Storage | O(unique pairs) | O(grant events) |
| Common query "is user X in tenant Y now?" | `filter(user, tenant).is_active` | `filter(user, tenant, is_active=True).exists()` |
| Common query "when did Y have access to X?" | Unrecoverable | `filter(user, tenant).values('granted_at', 'revoked_at')` |

Trade-off: storage cost in β scales with grant churn (low for our domain — clients don't churn salons hourly), audit value is high.

## Deactivation semantics (F2 — frozen + block new)

When TUR(user, tenant) flips to `is_active=False`:

1. **Existing appointments held by this pair** stay in the booking table. Status unchanged.
2. **New POST /appointments/** with `specialist.tenant=Y` and revoked TUR(user, Y) → 404 Variant B (same as Phase 0 path).
3. **Mobile app** sees the revoked tenant disappear from "your salons" list.
4. **Cancellation policy** applies normally to the held bookings.

Same shape for the specialist side: TUR(specialist_user, Y) revoked → specialist's profile remains accessible to held-booking clients, but doesn't appear in catalog / search for new bookings.

### Example walkthrough

- Apr 15: Anna grants TUR (via Variant E invisible-create) when booking Olga in Casa Bella.
- May 30: Casa Bella admin revokes Anna (alleged abuse). TUR.is_active=False, revoked_at=now, revoke_reason="abuse_report_42".
- Anna had a Jun 5 booking with Olga at the time of revoke. It stays. Anna can see it in her "День" tab.
- Anna tries to book another slot Jun 10. Cross-tenant 404 (membership absent).
- If Anna disputes and Casa Bella reinstates: new TUR row inserted, is_active=True, granted_at=now. Old row stays with its revoked_at = May 30. History intact.

## JWT integration (F3 — A: primary + X-Tenant override)

JWT continues to carry a single `tenant_id` claim. After #246 lands, this claim is the user's **primary** active tenant (highest-priority active relationship, ordered by `granted_at DESC` as a tiebreaker).

Pre-#246 JWTs (single-tenant world) carry the same `tenant_id` value. Post-#246 they remain backwards compatible — `user.tenant` FK becomes a *cached* pointer to the primary TUR for fast lookup.

Per-request override: `X-Tenant: roza` header sets `request.tenant=roza` for one request, regardless of JWT primary. Mobile app sets this when the user explicitly switches salon in the UI.

### Why not "list of tenant_ids in JWT" (Option C, rejected)

- JWT bloat: 50+ tenant ids in a token isn't far-fetched at marketplace scale (M5+).
- Refresh rotation overhead: every TUR change forces refresh.
- Mobile clients re-implement membership logic from token contents.

A simple "primary + per-request override" pattern preserves the JWT shape and pushes membership resolution to the DB.

## Variant E timing (F4 — Y: serializer-level membership check)

`AppointmentViewSet.permission_classes` drops `IsTenantMember`. Membership becomes a **booking-time** concern, owned by the serializer + service stack.

```python
# appointments/serializers.py — Variant E shape (replaces Phase 0 404)

class AppointmentCreateSerializer(serializers.Serializer):
    ...
    def validate(self, attrs):
        request = self.context["request"]
        request_tenant = getattr(request, "tenant", None)
        if request_tenant is None:
            return attrs

        specialist = SpecialistProfile.objects.only(
            "tenant_id",
        ).get(pk=attrs["specialist_id"])

        # Variant E: invisibly create TUR(user, specialist.tenant)
        # if absent. No consent dialog, no 404. The act of booking
        # is the consent gesture ("I want to use this salon").
        if specialist.tenant_id and specialist.tenant_id != request_tenant.id:
            # Cross-tenant grant inside the booking transaction.
            from users.models import TenantUserRelationship
            TenantUserRelationship.objects.get_or_create(
                user=request.user,
                tenant_id=specialist.tenant_id,
                is_active=True,
                defaults={"granted_by": "self"},
            )

        return attrs
```

### Why not `IsTenantMember` special-case (Option X, rejected)

`IsTenantMember` checks membership at the **permission** layer — too early in DRF's request pipeline to do business logic like "grant new TUR." Permission classes are sync, side-effect-free contracts. Granting a TUR is a side effect that belongs in the transaction.

### What stays + what changes

| Element | Pre-#246 | Post-#246 |
|---|---|---|
| `IsTenantMember` on `AppointmentViewSet` | ✓ | **dropped** |
| `IsTenantMember` on other viewsets (specialists / payments / reviews / schedule) | ✓ | ✓ (unchanged) |
| Queryset filter `qs.filter(tenant=request.tenant)` in `get_queryset` | ✓ | ✓ |
| Tenant stamping in `CreateBookingService._execute_atomic` | ✓ | ✓ |
| Schema null=False + CheckConstraint | ✓ | ✓ |
| Cross-tenant 404 in serializer (Phase 0) | ✓ | **replaced by invisible grant** |

Defense-in-depth posture is reduced on `AppointmentViewSet` only — the queryset filter remains the primary scoping guard. Adversarial reviewer will challenge this; founder ack F4=Y is the explicit decision.

## Privacy (F5 — default-deny + green zone)

When TUR(Anna, Casa Bella) is invisibly granted, Casa Bella admin/specialist dashboards see:

**Always visible (booking-essential):**
- Anna's `display_name`, `first_name`, `last_name`.
- Phone (E.164).
- Bookings WHERE `appointment.tenant_id = casa_bella.id` only.
- Aggregate visit count + last visit at THIS tenant only.

**Sensitivity-zoned (`UserPersonalContext`):**
- 🟢 **Green** — visible: workplace district, budget range, preferred service categories, favorite mastered they've already booked here.
- 🟡 **Yellow** — invisible: presence of children, partner schedule patterns, occupancy.
- 🔴 **Red** — invisible: pregnancy, chronic conditions, health markers.

Cross-tenant data isolation:
- Anna's bookings at Roza salon: invisible to Casa Bella admin.
- Anna's UserPersonalContext fields: scoped to fields whose zone permits visibility, not per-tenant access logs.
- Cross-tenant analytics queries: blocked at the queryset layer.

### Default-deny rationale

Aligns with `ayla-first-strategic-pivot`: AI is the user's. Each tenant gets just enough data to serve the user well at that tenant. Cross-tenant data sharing is opt-in (future M5+ feature), not opt-out.

## Migration plan

### Phase 1.A — Add model + backfill

1. `users/migrations/00XX_tenantuserrelationship.py`:
   - `CreateModel TenantUserRelationship` with the schema above.
   - `RunPython` backfills one TUR per existing `User.tenant` FK pair (granted_by='system', granted_at=user.date_joined or now).
2. Smoke: `TenantUserRelationship.objects.count() == User.objects.exclude(tenant=None).count()`.

### Phase 1.B — Refactor `IsTenantMember`

```python
# users/permissions.py — post-#246 shape
class IsTenantMember(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        request_tenant = getattr(request, "tenant", None)
        if request_tenant is None:
            return True  # permissive (rollout / opt-out paths)
        # Membership check via TUR, not User.tenant FK.
        return TenantUserRelationship.objects.filter(
            user=user, tenant=request_tenant, is_active=True,
        ).exists()
```

`User.tenant` FK kept as a "primary tenant" pointer for JWT minting + legacy reads; not the source of truth for membership.

### Phase 1.C — Drop `IsTenantMember` from `AppointmentViewSet`

Per F4=Y. Other viewsets keep it (different rationale: read endpoints don't need cross-tenant invisible grant).

### Phase 1.D — Implement Variant E in `AppointmentCreateSerializer.validate`

Replaces the Phase 0 404 guard.

### Phase 1.E — JWT minting reads from TUR primary

```python
# users/auth_serializers.py — token issuance
def get_token(cls, user):
    token = super().get_token(user)
    primary_tur = TenantUserRelationship.objects.filter(
        user=user, is_active=True,
    ).order_by("-granted_at").first()
    token["tenant_id"] = str(primary_tur.tenant_id) if primary_tur else None
    return token
```

Backwards-compat: pre-#246 JWTs (without TUR query) keep working — the X-Tenant header takes precedence; if both missing, permissive mode applies.

## Test coverage plan

| Class | Tests |
|---|---|
| `TestTUR_PartialUnique` | active uniqueness across (user, tenant); revoked rows allowed alongside; race on concurrent grant raises IntegrityError; re-grant after revoke creates new row. |
| `TestTUR_DeactivationSemantics` | revoke leaves existing appointments intact; new POST /appointments/ → 404 after revoke; re-grant restores access. |
| `TestIsTenantMember_TUR_Refactor` | membership granted via TUR; permission False when no active TUR; permission True when revoked + new TUR; permissive when no X-Tenant header. |
| `TestVariantE_InvisibleGrant` | booking specialist in tenant B from tenant A context auto-creates TUR(client, B); subsequent operations succeed; revoke + re-book creates new TUR row (history). |
| `TestJWT_TUR_Primary` | new JWTs carry primary TUR's tenant_id; X-Tenant overrides per-request; pre-#246 JWTs (legacy claim) remain valid. |
| `TestPrivacy_DefaultDeny` | tenant admin's dashboard view of cross-tenant user shows only green-zone fields + tenant-scoped bookings. |
| `TestMigration_Backfill` | backfill creates 1 TUR per User.tenant; idempotent on rerun; counts match. |

## Adversarial flags (mandatory pre-merge §H.3)

When the implementation PR opens, adversarial reviewer should focus on:

1. **`get_or_create` race in Variant E** — two concurrent POSTs from Anna for Olga (different services) → both pass validator → race on TUR creation. Partial unique constraint catches via IntegrityError; verify get_or_create handles this without 500.
2. **JWT primary stability** — when primary TUR is revoked, what happens to in-flight JWTs? They still carry the old primary's tenant_id → `IsTenantMember` rejects → 403. Acceptable: client refreshes token, gets new primary. Document.
3. **Cross-tenant write via revoked-but-cached-JWT** — Anna's JWT carries tenant_id=casa, X-Tenant=casa, but Casa Bella revoked her TUR 30 seconds ago. Request lands while JWT is valid. Membership check via TUR rejects → 403. ✓
4. **Specialist's TUR vs Appointment.tenant_id** — when specialist moves tenant (TUR(user_spec, A) revoked, TUR(user_spec, B) created), what happens to her client's appointments in A? Per F2 they stay. The queryset filter in tenant A still returns them (Appointment.tenant_id=A unchanged). Specialist sees them via her access to tenant A's catalog... wait, does she lose tenant A access on revoke? Yes. So the appointments become orphan-visible (clients can see; specialist can't). Open question for product.
5. **Mass-revoke (tenant deletion)** — Tenant.on_delete=PROTECT means tenants can't be deleted while TUR rows exist. Soft-delete tenant → bulk-revoke all TURs in a transaction. Design.

## Out of scope (separate tickets)

- Per-tenant `UserPersonalContext` slicing — needs `personalisation_zone` field on context fields + tenant-aware queries. M5+.
- Cross-tenant analytics dashboard (founder ack for tenant_id list-views). M5+.
- Tenant-switching UX in the mobile app (explicit "switch salon" menu). Mobile team.

## References

- ADR-0009 §Hard rule #6 (tenant_id is `active_tenant_id`).
- PR #147 — cross-tenant create reject design doc (founder acked).
- PR #148 — Phase 0 Variant B 404 interim (in flight).
- PR #142 (#520) — `IsTenantMember` origin.
- PR #144 (#568) — `Appointment.tenant_id` data backfill.
- PR #145 (#590) — schema null=False + CheckConstraint.
- ayla-first-strategic-pivot — "AI is the user's" framing.
- 152-ФЗ — Russian personal-data law (access-log requirement).

## Open questions for founder

1. **TUR revoke notifications.** When Anna's TUR is revoked by Casa Bella admin, does she get a push? An email? Silent revoke? Privacy vs UX trade-off.
2. **Specialist mobility** (raised in PR #147 doc, still open). When a master moves tenant A → B, do existing bookings auto-cancel, follow her, or stay in A as orphan-visible?
3. **TUR primary tiebreak** — currently `granted_at DESC`. Should be "most-recently-active" (last successful booking)? Affects which tenant gets the JWT.
4. **Mass migration risk** — Track A says #246 lands in bot-platform first. Are downstream consumers expected to consume `tenant_id` from JWT only, or also from a separate /me/relationships endpoint?

When founder answers, the implementation PR scope tightens. Without these, default to: silent revoke, frozen (per F2), granted_at DESC, JWT-only for downstream.
