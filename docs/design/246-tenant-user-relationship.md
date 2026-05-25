# #246 — TenantUserRelationship Model + Sprint 1 Track A integration

**Status:** ✅ design fully decided 2026-05-25. Implementation ready to start.
**Author:** Stream Alpha (after closing 9 layers of ADR-0009 §6 + Phase 0 cross-tenant 404).
**Audience:** Stream Alpha (implementation) + Gamma (downstream impact).
**Issue:** ai-bot-platform Sprint 1 Track A #246.
**Related PRs:** PR #142 (#520), #144 (#568), #145 (#590), PR #147 (cross-tenant create options, merged), PR #148 (Phase 0 cross-tenant 404, merged).

## TL;DR (post founder ack 2026-05-25)

Replace `User.tenant` single FK with a many-to-many `TenantUserRelationship` table. **Customer is multi-provider by default** — no automatic primary tenant. Tenant context lives in `X-Tenant` header per-request, not as a default JWT claim for customers.

Decisions ack-ed:
- **F1 schema:** β (partial unique on `(user, tenant) WHERE is_active=True` + history rows for 152-ФЗ audit).
- **F2 deactivation:** frozen existing bookings + block new. **Master who "slams the door" — salon must fulfil obligations** (find replacement) and notify the client of master change.
- **F3 JWT:** customer's JWT carries `active_tenant_id=null` by default; X-Tenant header is the per-request context source. **No "primary tenant" auto-pick for customer.** Staff (master/admin) JWT carries their staff-tenant claim.
- **F4 Variant E timing:** `IsTenantMember` stays on `AppointmentViewSet` in **permissive** mode (passes when `request.tenant=None`); Variant E invisible-grant moves into `AppointmentCreateSerializer.validate`. Both layers coexist — permission is rollout-friendly, serializer enforces booking-time decision.
- **F5 privacy:** default-deny across tenants + green-zone visibility (per `UserPersonalContext` sensitivity zones).
- **Q1 revoke notification:** silent + 404 on next attempt.
- **Q2 specialist mobility:** bookings frozen in old tenant; old tenant must honour obligations (find replacement) and notify client on master substitution.
- **Q3 JWT tiebreak:** N/A — no primary tenant for customers.
- **Q4 downstream consumer access:** `GET /api/v1/users/me/tenant-relationships/` is the canonical source for the full list. JWT carries optional `active_tenant_id` only when explicitly set.

## Multi-provider customer model (canonical framing)

The customer is bound to N providers simultaneously by default — massage at salon A, nails at salon B, lashes from a freelance master, cosmetology at salon C. **Each link is permanent and equal-weight.** No "main salon" concept for customers.

`active_tenant_id` = **context of a specific action**, not a property of the customer in general:

- **Global customer-wide actions** (no tenant required, `request.tenant=None`):
  - Profile (`/me/`), AI memory (`/me/personal-context/`), food scanner, water tracker, global appointments list, marketplace specialist search, `/ai/chat/`.
- **Provider-scoped actions** (require `X-Tenant` header OR derivable from specialist_id):
  - Open salon card, list a specialist's services, create booking, cancel/reschedule, leave review.
- **Staff actions** (require tenant; staff tenant must match `request.tenant`):
  - Schedule management, services CRUD, complete/no_show, admin operations.

The existing infrastructure (`TenantContextMiddleware` permissive when X-Tenant absent, `IsTenantMember.has_permission` returns True when `request.tenant=None`, queryset filter `if request_tenant is not None: qs.filter(tenant=...)`) **already aligns with this model** — most refactor effort is documentation + JWT-mint cleanup, not new code paths.

## Why now

Phase 0 fix (PR #148 — cross-tenant 404 info-hiding) closes the immediate integrity bug but breaks the pilot growth funnel: friend-of-friend share links between Penza salons return "Specialist not found." Pre-pilot is the right window to land #246 + Variant E so by 2026-07-15 launch the funnel works.

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

## JWT integration (F3 refined — no primary tenant for customer)

**Customer JWT** carries `active_tenant_id=null` by default. Tenant context comes from the `X-Tenant` header per-request. The customer is multi-provider by definition; we don't artificially elect a "main salon."

**Staff JWT** (specialist / admin) carries `active_tenant_id = staff_tenant.id` — they need a default context because most of their endpoints (schedule, services, complete/no_show) are tenant-scoped. Multi-tenant staff (post-Variant-E specialist mobility) need explicit "switch tenant" UI at login.

```python
# users/auth_serializers.py — post-#246 token minting

def get_token(cls, user):
    token = super().get_token(user)
    if user.role == "client":
        # Multi-provider by default — no primary tenant context.
        token["active_tenant_id"] = None
    else:
        # Staff need a default tenant. If they have 1 active staff
        # relationship — use it. If N — mobile UI selects at login.
        primary = TenantUserRelationship.objects.filter(
            user=user, is_active=True, role="staff",
        ).order_by("-granted_at").first()
        token["active_tenant_id"] = (
            str(primary.tenant_id) if primary else None
        )
    return token
```

Per-request override: `X-Tenant: roza` header sets `request.tenant=roza` for one request. Mobile sends it only for provider-scoped actions (open salon card, book, cancel). Global actions (profile, AI memory, marketplace search, global bookings list) omit the header.

### Why not "list of tenant_ids in JWT" (Option C, rejected)

- JWT bloat: 50+ tenant ids in a token isn't far-fetched at marketplace scale (M5+).
- Refresh rotation overhead: every TUR change forces refresh.
- Mobile clients re-implement membership logic from token contents.

### Why not "primary tenant for customer" (original F3=A, superseded)

Original draft proposed `granted_at DESC` to elect a customer primary. Founder corrected: a customer with massage at salon A + nails at salon B + lashes at freelance master + cosmetology at salon C has **no business-meaningful "main"**. Each link is permanent and equal-weight. Artificially picking primary distorts the data model.

`GET /api/v1/users/me/tenant-relationships/` is the canonical full-list endpoint (Q4 closure).

## Endpoint classification (action-context model)

| Group | Endpoints | X-Tenant required | `IsTenantMember` mode |
|---|---|---|---|
| **Global customer-wide** | `/me/`, `/me/personal-context/`, `/me/tenant-relationships/`, `/me/appointments/` (cross-tenant list), `/nutrition/*`, `/water/*`, `/search/`, `/specialists/` (marketplace), `/ai/chat/`, `/auth/*` | No (permissive) | Permissive when `request.tenant=None` |
| **Provider-scoped (client side)** | `/specialists/{id}/`, `/services/{id}/`, `/appointments/` (POST create), `/appointments/{id}/cancel`, `/appointments/{id}/reschedule`, `/reviews/`, `/specialists/{id}/slots/`, `/payments/create`, `/payments/{id}/retry/` | Yes (header OR derivable from specialist_id) | Enforced |
| **Staff** | `/specialists/me/schedule/`, `/services/` (CRUD), `/appointments/{id}/complete`, `/appointments/{id}/no-show`, `/payments/{id}/refund` | Yes (staff_tenant must match) | Enforced + staff-role check |

## Variant E timing (F4 refined — coexist permissive + serializer-level)

**Updated 2026-05-25:** original F4=Y ack was "drop `IsTenantMember` from `AppointmentViewSet`". With the multi-provider customer model, the cleaner shape is:

- **Keep `IsTenantMember`** in `AppointmentViewSet.permission_classes` — but it runs in **permissive** mode (`request.tenant=None` → returns True). This preserves the global appointments list path (`GET /me/appointments/` without X-Tenant).
- **Variant E moves to `AppointmentCreateSerializer.validate`** — runs only on POST create, has request context, can do the invisible-grant side effect inside the booking transaction.

The two layers don't conflict: permission is a request-time gate (cheap, permissive), serializer is a booking-time decision (mutation, atomic). This matches the existing patterns in `payments/views.py` and `reviews/views.py` already.

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
| `IsTenantMember` on `AppointmentViewSet` | enforced | **permissive when `request.tenant=None`** (global appointments list works); enforced when tenant is set |
| `IsTenantMember` on other viewsets (specialists / payments / reviews / schedule) | ✓ | ✓ (unchanged) |
| Queryset filter `qs.filter(tenant=request.tenant)` in `get_queryset` | ✓ | ✓ (unchanged — already permissive when no tenant) |
| Tenant stamping in `CreateBookingService._execute_atomic` | ✓ | ✓ |
| Schema null=False + CheckConstraint | ✓ | ✓ |
| Cross-tenant 404 in serializer (Phase 0) | ✓ | **replaced by Variant E invisible grant** |

Defense-in-depth posture preserved on `AppointmentViewSet` — `IsTenantMember` still gates the request layer (permissive when no tenant context, enforced when X-Tenant present). Variant E runs at serializer layer to grant TUR if missing.

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

`User.tenant` FK becomes **legacy** for customer rows (no semantic meaning — multi-provider by default). For staff rows, the FK stays as a denormalized "primary staff tenant" pointer for fast JWT minting. Drop the FK from customer rows is a Sprint 2 cleanup (low-priority refactor; safer to deprecate-in-place).

### Phase 1.C — `AppointmentViewSet` permission update

Per F4 refined ack 2026-05-25. Permission stays as the request-time gate; Variant E owns the booking-time decision. They coexist.

### Phase 1.D — Implement Variant E in `AppointmentCreateSerializer.validate`

Replaces the Phase 0 404 guard. The validator calls `TenantUserRelationship.objects.get_or_create(...)` inside the booking transaction. If specialist's tenant matches request.tenant — no-op. If different — invisible grant.

### Phase 1.E — JWT minting (role-aware, no primary for customer)

```python
# users/auth_serializers.py — post-#246 shape
def get_token(cls, user):
    token = super().get_token(user)
    if user.role == "client":
        token["active_tenant_id"] = None  # multi-provider; no default
    else:
        # staff (master/admin) — need default tenant context
        primary = TenantUserRelationship.objects.filter(
            user=user, is_active=True, role="staff",
        ).order_by("-granted_at").first()
        token["active_tenant_id"] = (
            str(primary.tenant_id) if primary else None
        )
    return token
```

Backwards-compat: pre-#246 JWTs (carrying `tenant_id` legacy claim) keep working — `TenantContextMiddleware` falls back to the JWT claim when X-Tenant header is absent.

### Phase 1.F — Add `GET /api/v1/users/me/tenant-relationships/`

```python
# users/views.py — new endpoint (Q4 closure)
class MyTenantRelationshipsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        rows = (
            TenantUserRelationship.objects
            .filter(user=request.user, is_active=True)
            .select_related("tenant")
            .order_by("-granted_at")
        )
        return success_response({
            "data": [
                {
                    "tenant_id": str(row.tenant_id),
                    "tenant_slug": row.tenant.slug,
                    "tenant_name": row.tenant.name,
                    "granted_at": row.granted_at.isoformat(),
                    "role": row.role,
                }
                for row in rows
            ],
        })
```

Canonical full-list endpoint per Q4 closure. Bot-platform and mobile clients call this to populate "your providers" UI.

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

- ADR-0009 §Hard rule #6 (tenant_id is `active_tenant_id`, action context not user property).
- PR #147 — cross-tenant create reject design doc (finalized + merged 2026-05-25, commit `a394c9b0`).
- PR #148 — Phase 0 Variant B 404 interim (merged, commit `a8db0247`).
- PR #142 (#520) — `IsTenantMember` origin.
- PR #144 (#568) — `Appointment.tenant_id` data backfill.
- PR #145 (#590) — schema null=False + CheckConstraint.
- `ayla-first-strategic-pivot` — "AI is the user's" framing — drives the multi-provider customer model.
- 152-ФЗ — Russian personal-data law (access-log requirement).

## Closed questions log (founder ack 2026-05-25)

| Q | Question | Decision | Implementation impact |
|---|---|---|---|
| Q1 | TUR revoke notification — push / email / silent? | **Silent + 404 on next attempt.** Customer learns via failed booking. | No `OutboxEvent.Topic.TENANT_USER_REVOKED`, no notification template. Admin endpoint `POST /admin/tenants/{id}/revoke-user/` takes no `notify` flag. |
| Q2 | Specialist mobility — stay / cancel / follow? | **Stay (frozen).** Master must fulfil obligations. If master "slams the door" out, **salon must find replacement and notify the client of master change.** | Cancellation policy gains "tenant must provide replacement within 24h or refund" path. New notification template `appointment_specialist_replaced` (client side). No data migration of `Appointment.tenant_id` — CheckConstraint #590 stays. |
| Q3 | JWT primary tiebreak | **N/A — no primary for customer.** Customer JWT carries `active_tenant_id=null`. Staff JWT carries staff-tenant. | Token minting branches on `user.role`. |
| Q4 | Downstream consumer access — JWT-only or endpoint? | **Hybrid:** JWT carries optional `active_tenant_id` (staff only). `GET /api/v1/users/me/tenant-relationships/` is canonical full-list endpoint. | Implements Phase 1.F above. |

### Still pending (recommended defaults — implementation can proceed)

- **Q5 (raised by Stream Alpha):** one `TenantUserRelationship` model with `role` field (`customer`/`staff`/`admin`) vs two separate models (`CustomerTenantLink` + `StaffTenantLink`)?
  - **Recommended:** one model + `role` field. DRY, simpler reporting queries, easier migration. Staff-specific side-fields (`hire_date`, `commission_rate`) live on a separate `StaffEmployment` model with FK to TUR — keeps customer rows lean.
  - **Founder ack pending; not blocking implementation start** (schema can evolve in sub-phase 1 PR).
