"""Custom permission classes for BeautyGO API."""

from hmac import compare_digest
from typing import Any

from django.conf import settings
from rest_framework import permissions


class IsClientApp(permissions.BasePermission):
    """Allow only requests from BeautyGO client app (X-App-Type: client)."""
    message = "Этот эндпоинт доступен только из приложения BeautyGO"

    def has_permission(self, request: Any, view: Any) -> bool:
        return getattr(request, 'app_type', None) == 'client'


class IsProApp(permissions.BasePermission):
    """Allow only requests from BeautyGO Pro app (X-App-Type: pro)."""
    message = "Этот эндпоинт доступен только из приложения BeautyGO Pro"

    def has_permission(self, request: Any, view: Any) -> bool:
        return getattr(request, 'app_type', None) == 'pro'


class IsClient(permissions.BasePermission):
    """Allow access only to **registered** users with role=client.

    Anonymous users (created via ``POST /auth/anonymous``) carry an
    ``is_guest=True`` flag and a default ``role='client'`` so they can
    browse the catalogue. Endpoints guarded by ``IsClient`` — payments,
    reviews, food scanner, home screen, personal context — are for
    registered accounts only; anon must verify OTP first to merge into
    a real account.

    Surfaced by smoke-test against dev VPS 2026-04-27: anon was reaching
    /home/ and /personal-context/ because the original check only looked
    at role. The Gate model in spec v2.0 treats those as gated actions.
    """

    def has_permission(self, request: Any, view: Any) -> bool:
        user = request.user
        return (
            user.is_authenticated
            and user.role == 'client'
            and not getattr(user, 'is_guest', False)
        )


class IsSpecialist(permissions.BasePermission):
    """Allow access only to users with role=specialist."""

    def has_permission(self, request: Any, view: Any) -> bool:
        return (
            request.user.is_authenticated
            and request.user.role == 'specialist'
        )


class IsTenantMember(permissions.BasePermission):
    """Allow only callers whose user belongs to ``request.tenant`` (DRF-242.4).

    Used together with ``TenantContextMiddleware`` (which resolves the
    ``X-Tenant`` header into ``request.tenant``). The permission compares
    the resolved tenant against ``request.user.tenant`` and rejects the
    mismatch case — preventing one tenant's authenticated user from
    reaching another tenant's data simply by changing the header.

    Behavior matrix:
    | request.tenant | user.tenant | result                |
    |----------------|-------------|------------------------|
    | None           | any         | True (legacy / strict-mode-off path) |
    | T1             | None        | False (user not yet backfilled — 403 protects against escalation) |
    | T1             | T1          | True                   |
    | T1             | T2          | False                  |

    Anonymous users (no JWT) are rejected — combine with ``IsAuthenticated``
    if you need to express "must-be-authenticated AND must-match-tenant".
    DRF-242.5's ``MULTI_TENANT_STRICT`` will tighten the None/None and
    None/T1 rows once the backfill has run.
    """

    message = "Доступ к ресурсам этого тенанта запрещён"

    def has_permission(self, request: Any, view: Any) -> bool:
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        request_tenant = getattr(request, "tenant", None)
        # No header → caller didn't ask for tenant scope. Permissive
        # by design (#246: customer is multi-provider; global endpoints
        # work without tenant context). The middleware's
        # MULTI_TENANT_STRICT gate handles header-required paths.
        if request_tenant is None:
            return True
        # #246 sub-phase 1.B: membership read from TenantUserRelationship.
        # User.tenant FK is now a denormalized pointer; the source of
        # truth is the TUR table. `User.post_save` bridges legacy
        # `user.tenant=X` callsites by auto-granting TUR.
        from users.models import TenantUserRelationship
        return TenantUserRelationship.objects.filter(
            user=user,
            tenant=request_tenant,
            is_active=True,
        ).exists()


class IsServiceAccount(permissions.BasePermission):
    """Allow only service-to-service calls authenticated by a shared secret.

    Used for `/api/v1/nutrition/internal/*` endpoints — MAX bot calls Ayla
    on behalf of a BotUser. Caller passes `X-Service-Token` header; we compare
    against `settings.NUTRITION_SERVICE_TOKEN` in constant time.

    Caller MUST also pass `X-External-User-ID` (e.g. `bot:12345`) so the view
    can resolve to a ProxyUser via `users.services.resolve_external_user`.
    Validation of the header presence is done by the view, not here — this
    permission only guards the auth boundary.
    """

    message = "Service-to-service auth required"

    def has_permission(self, request: Any, view: Any) -> bool:
        expected = getattr(settings, "NUTRITION_SERVICE_TOKEN", "") or ""
        if not expected:
            # Misconfigured deployment — fail closed.
            return False
        provided = request.META.get("HTTP_X_SERVICE_TOKEN", "")
        if not provided:
            return False
        return compare_digest(provided, expected)


class IsBotServiceWithVerifiedClient(permissions.BasePermission):
    """Bearer-authenticated service-to-service calls with a resolved actor.

    Used by `/api/v1/payments/internal/*` (task #85) and other endpoints
    where the bot acts on behalf of a specific Ayla User and the view
    still needs ``request.user`` to be that User (so existing filters
    like ``appointment__client=request.user`` keep working unchanged).

    Auth contract:

    1. ``Authorization: Bearer <token>`` matches
       ``settings.AYLA_INTERNAL_API_TOKEN`` (constant-time). Empty
       setting fails closed — never accept any token on a misconfigured
       deployment.
    2. ``X-External-User-ID`` (e.g. ``bot:12345``) resolves via
       ``resolve_external_user`` to an Ayla ``User`` (lazily created
       as a proxy on first call — same semantics as nutrition/internal).
    3. ``request.user`` is replaced by the resolved User so views can
       use the same per-user queryset filters as the mobile path.

    Defense-in-depth (lives in the **view**, not here): the view MUST
    cross-check the request body's ``client_id`` field against
    ``request.user.id``. The bearer token alone is a single secret;
    if it leaks, an attacker holding it could impersonate any user by
    forging only the header. Forcing the body to independently name
    the same user means a leaked token still requires the attacker to
    also know the victim's specific Ayla user-id — a second factor
    that limits blast radius.

    Why this is not just ``IsServiceAccount`` with extra steps: that
    class deliberately leaves user resolution to the view (nutrition
    internal endpoints accept multiple shapes); this one promises that
    a passing permission guarantees ``request.user`` is a concrete,
    resolved Ayla user. Views written against this class can rely on
    ``request.user`` being non-anonymous without re-checking.
    """

    message = "Bot service auth required"

    def has_permission(self, request: Any, view: Any) -> bool:
        # Lazy import — users.services imports models, breaking the
        # users.permissions → users.services circular if hoisted.
        from users.services import (
            InvalidExternalUserIDError,
            resolve_external_user,
        )

        expected = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
        if not expected:
            # Misconfigured deployment — never honour any bearer.
            return False

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return False
        provided = auth_header[len(prefix):].strip()
        if not provided or not compare_digest(provided, expected):
            return False

        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        if not external_user_id:
            return False
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError:
            return False

        # Replace AnonymousUser (the request authenticator never ran
        # for this permission-only auth path) with the resolved Ayla
        # User. Downstream code — including the view's defense-in-depth
        # cross-check — reads request.user.
        request.user = user
        return True


class IsInternalBearer(permissions.BasePermission):
    """Bearer-authenticated service-to-service calls without user resolution.

    Used by admin-level batch endpoints (task #92 `POST
    /api/v1/masters/internal/by-yclients-staff-ids/`) where the bot
    acts on behalf of an *admin operator*, not a specific Ayla User.
    No ``X-External-User-ID`` is required; ``request.user`` stays
    Anonymous.

    Auth contract:

    1. ``Authorization: Bearer <token>`` matches
       ``settings.AYLA_INTERNAL_API_TOKEN`` (constant-time). Empty
       setting fails closed.

    When the endpoint is per-tenant, the **view** still enforces the
    tenant boundary by requiring an explicit ``tenant_id`` field in
    the request body — same defense-in-depth idea as ``client_id`` in
    ``IsBotServiceWithVerifiedClient``: a leaked bearer can't be used
    to enumerate across tenants without naming each one explicitly.

    Pick this over ``IsBotServiceWithVerifiedClient`` when the endpoint
    is *catalog-shaped* (read/lookup across many records) rather than
    *on-behalf-of-user-shaped* (write tied to one User's data). The
    distinction matters because user-shaped endpoints have a natural
    second factor (the user-id); catalog endpoints don't.
    """

    message = "Internal service auth required"

    def has_permission(self, request: Any, view: Any) -> bool:
        expected = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
        if not expected:
            return False
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return False
        provided = auth_header[len(prefix):].strip()
        if not provided or not compare_digest(provided, expected):
            return False
        return True


class IsIdentityProvisioningBearer(permissions.BasePermission):
    """Provisioning-only Bearer for the identity-binding endpoint.

    Used ONLY by ``POST /api/v1/internal/users/bind-external/``
    (E2E-BOT-02B hardening). Identity binding takes a caller-named
    ``(external_user_id, ayla_user_id)`` pair with no server-side proof
    of ownership, so it must NOT be reachable by the standard BOT
    runtime credential: this class checks
    ``settings.AYLA_IDENTITY_PROVISIONING_TOKEN``, a secret provisioned
    independently of ``AYLA_INTERNAL_API_TOKEN`` and never deployed to
    the bot service.

    Auth contract:

    1. ``Authorization: Bearer <token>`` matches
       ``settings.AYLA_IDENTITY_PROVISIONING_TOKEN`` (constant-time).
       Empty setting fails closed — the endpoint is disabled until ops
       explicitly provisions the credential.
    2. A valid ``AYLA_INTERNAL_API_TOKEN`` is REJECTED here by
       construction (different setting, different value).
    3. Misconfiguration hard-fail: if the two settings hold the SAME
       non-empty value, every request is denied (and system check
       ``users.E001`` fails at boot) — the boundary must not depend on
       ops discipline alone.

    Production bot-driven binding is not supported until a verified
    ownership flow exists (AYLA-DEC-0016 §6: relink only for verified
    identity references). Until then the endpoint serves trusted
    provisioning / E2E bootstrap / ops only.
    """

    message = "Identity provisioning auth required"

    def has_permission(self, request: Any, view: Any) -> bool:
        expected = getattr(settings, "AYLA_IDENTITY_PROVISIONING_TOKEN", "") or ""
        if not expected:
            return False
        general = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
        if general and compare_digest(expected, general):
            # Equal-values misconfiguration: the general bot credential
            # would pass. Fail closed rather than trusting ops to keep
            # the two secrets distinct.
            return False
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return False
        provided = auth_header[len(prefix):].strip()
        if not provided or not compare_digest(provided, expected):
            return False
        return True


class IsTenantAdmin(permissions.BasePermission):
    """Caller must hold an active ``admin``-role TUR in ``request.tenant``.

    Used by admin-only endpoints in the salon-management surface
    (#246 Q1 revoke endpoint, future master-departure flow). The
    permission requires BOTH:

    1. ``request.user`` is authenticated.
    2. ``request.tenant`` is non-None (X-Tenant header must be set;
       ``TenantContextMiddleware`` resolved it).
    3. A ``TenantUserRelationship`` row exists with
       ``user=request.user``, ``tenant=request.tenant``,
       ``role=admin``, ``is_active=True``.

    A salon admin can act ONLY inside the tenant they administer. The
    middleware's ``request.tenant`` is the *active* tenant (from the
    header / JWT claim); the TUR check confirms the user is registered
    as admin THERE — not just somewhere else in the system. This is
    the second factor that defeats the "admin-of-A acts on tenant-B"
    impersonation attempt.
    """

    message = "Доступ только для администратора салона"

    def has_permission(self, request: Any, view: Any) -> bool:
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        request_tenant = getattr(request, "tenant", None)
        if request_tenant is None:
            return False
        from users.models import TenantUserRelationship
        return TenantUserRelationship.objects.filter(
            user=user,
            tenant=request_tenant,
            role=TenantUserRelationship.Role.ADMIN,
            is_active=True,
        ).exists()


class IsTenantAdminOrPlatformAdmin(permissions.BasePermission):
    """Salon administrator of THIS tenant, or Ayla platform staff (DRF-1062).

    The salon-admin surface has two legitimate actors and one shared rule:
    the tenant is always taken from ``request.tenant`` (X-Tenant header or
    the JWT claim, resolved by ``TenantContextMiddleware``), never from the
    request body. A platform operator therefore has to name the salon it is
    acting on, exactly like a salon admin does — the flag widens *which*
    tenants may be addressed, not how many at once.

    Deliberately not honouring ``is_superuser``: DRF-1025 records that the
    Django superuser has no tenant limits at all, and inheriting that here
    would reintroduce the defect this surface exists to avoid.
    """

    message = "Доступ только для администратора салона или платформы"

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False

        request_tenant = getattr(request, "tenant", None)
        if request_tenant is None:
            # No addressed salon → nothing to authorise against, for
            # either actor. Fail closed.
            return False

        if getattr(user, "is_platform_admin", False):
            return True

        from users.models import TenantUserRelationship
        return TenantUserRelationship.objects.filter(
            user=user,
            tenant=request_tenant,
            role=TenantUserRelationship.Role.ADMIN,
            is_active=True,
        ).exists()
