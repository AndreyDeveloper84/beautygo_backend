"""Custom permission classes for BeautyGO API."""

import logging
from hmac import compare_digest
from typing import Any

from django.conf import settings
from rest_framework import permissions

logger = logging.getLogger(__name__)


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
    (E2E-BOT-02B hardening). DRF-1525 briefly put ``POST
    /api/v1/internal/tenants/`` behind this same class on the argument
    «заведение салона — та же сила»; that was wrong (DRF-1695, C1):
    creating a salon claims no identity, and one secret for two powers
    handed the bot the binding power §151 forbids. Tenants now sit behind
    :class:`IsTenantProvisioningBearer`. Identity binding takes a caller-named
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


class IsTenantProvisioningBearer(permissions.BasePermission):
    """Provisioning-only Bearer for ``POST /api/v1/internal/tenants/`` (DRF-1695).

    A power of its own — «завести салон по slug» — held by the bot's
    «подключить салон» screen (actor: the bot admin's superuser, a named
    human), and deliberately NOT the identity-binding power: the two
    secrets differ, and system checks ``users.E002``/``E003`` refuse to boot
    when ops sets them equal.

    Auth contract mirrors :class:`IsIdentityProvisioningBearer`: constant-time
    Bearer match against ``settings.AYLA_TENANT_PROVISIONING_TOKEN``; empty
    setting fails closed; a value equal to the general Bearer or to the
    identity secret is refused per request as well as at boot.

    Transition (one deploy, DRF-1695 step 1→3): while the tenant secret is
    still EMPTY, the identity secret is accepted here so the form the owner
    is using today keeps working until ops provisions the new value. The
    moment ``AYLA_TENANT_PROVISIONING_TOKEN`` is set, the identity secret
    stops opening this route — no flag to forget, the cut-over is the
    presence of the new value.
    """

    message = "Tenant provisioning auth required"

    def has_permission(self, request: Any, view: Any) -> bool:
        general = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
        identity = getattr(settings, "AYLA_IDENTITY_PROVISIONING_TOKEN", "") or ""
        expected = getattr(settings, "AYLA_TENANT_PROVISIONING_TOKEN", "") or ""
        if not expected:
            # Transition only: identity secret opens tenants until the
            # dedicated one is provisioned. Still never the general Bearer.
            expected = identity
        if not expected:
            return False
        if general and compare_digest(expected, general):
            return False
        tenant = getattr(settings, "AYLA_TENANT_PROVISIONING_TOKEN", "") or ""
        if tenant and identity and compare_digest(tenant, identity):
            # Equal secrets = one power again. Fail closed (users.E003 at boot).
            return False
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return False
        provided = auth_header[len(prefix):].strip()
        if not provided or not compare_digest(provided, expected):
            return False
        return True


class ServiceCredentialIsReadOnly(permissions.BasePermission):
    """A request authenticated by the Ayla service Bearer may only read.

    The Ayla read grant is expressed as an authenticator
    (``users.authentication.AylaServiceBearerAuthentication``) so the same
    view can keep serving humans over JWT. An authenticator, unlike a
    permission, cannot express "GET but not PUT" -- and four of the salon
    surfaces it is attached to are mixed classes (``get`` + ``put`` /
    ``post`` under one ``permission_classes``). Without this class,
    granting Ayla the read half of ``AdminScheduleView`` would silently
    grant it ``PUT`` on the weekly template too.

    So the method rule lives here, next to the other permissions, and
    keys off *how the caller authenticated* rather than off the view:

    * authenticated by the service Bearer -> ``GET``/``HEAD``/``OPTIONS``
      only, everything else is refused;
    * authenticated any other way (a salon administrator's JWT) ->
      unchanged, this class abstains.

    It only ever subtracts. Adding it to a view cannot widen access, so
    it is safe to list on every view the authenticator touches, and the
    owner's rule -- "``GET``/``HEAD``/``OPTIONS`` may be opened;
    ``POST``/``PUT``/``PATCH``/``DELETE`` must not open automatically" --
    holds by construction rather than by remembering to split views.

    Why not simply give Ayla its own read-only secret instead: the bot
    already holds ``AYLA_INTERNAL_API_TOKEN`` and already has the salon
    write surface under it, so a second secret for the same consumer
    would add an ops artefact without removing any capability from the
    holder of the first. What actually keeps this read grant safe is that
    the tenant is still proved by ``IsTenantAdmin`` against the *human*
    named in ``X-External-User-ID`` -- a stolen token alone cannot name an
    arbitrary salon in ``X-Tenant`` and read it.
    """

    message = "Service credential is read-only on this surface"

    def has_permission(self, request: Any, view: Any) -> bool:
        from users.authentication import AylaServiceBearerAuthentication

        authenticator = getattr(request, "successful_authenticator", None)
        if isinstance(authenticator, AylaServiceBearerAuthentication):
            return request.method in permissions.SAFE_METHODS
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


# ---------------------------------------------------------------------------
# DRF-1617 / B-2.1 — the internal token stops meaning "any subject".
# ---------------------------------------------------------------------------


class _CredentialPurpose:
    """Which credential the caller presented, by the purpose it was issued for."""

    NONE = "none"
    INTERNAL = "internal"          # AYLA_INTERNAL_API_TOKEN — runtime bot credential
    PROVISIONING = "provisioning"  # AYLA_IDENTITY_PROVISIONING_TOKEN — ops only
    UNKNOWN = "unknown"            # a bearer that matches nothing we issued


def _bearer(request: Any) -> str:
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return ""
    return auth_header[len(prefix):].strip()


def _credential_purpose(request: Any) -> str:
    """Classify the presented credential by PURPOSE, not by validity.

    ``IsInternalBearer`` folds every non-runtime outcome into one ``False``.
    Enough to keep the door shut, not enough to say what knocked. Here the
    provisioning credential is recognised explicitly: presenting an ops-only
    secret to a runtime surface is a misdirected purpose, not an invalid
    token — opposite fixes, identical from outside.

    Constant-time comparison; an unset setting is skipped, never compared.
    """
    provided = _bearer(request)
    if not provided:
        return _CredentialPurpose.NONE
    internal = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
    if internal and compare_digest(provided, internal):
        return _CredentialPurpose.INTERNAL
    provisioning = getattr(settings, "AYLA_IDENTITY_PROVISIONING_TOKEN", "") or ""
    if provisioning and compare_digest(provided, provisioning):
        return _CredentialPurpose.PROVISIONING
    return _CredentialPurpose.UNKNOWN


class IsInternalBearerForSubject(permissions.BasePermission):
    """Runtime internal bearer, restricted to the caller's OWN subject.

    Attach to any internal view whose URL names a person, and declare which
    kwarg holds that person::

        class InternalPersonalDataExportView(APIView):
            permission_classes = [IsInternalBearerForSubject]
            subject_url_kwarg = "user_id"

    A view that forgets ``subject_url_kwarg`` is refused, loudly (ERROR).
    Silence would be the one failure this class exists to prevent: an
    unguarded surface that looks guarded because the class name is in the
    list.

    ### What is checked, in order

    1. **Purpose.** Only the runtime credential passes. The provisioning
       credential is recognised and refused *as such*.
    2. **Named.** ``X-External-User-ID`` is required. Since ai-bot-platform
       #1535 (11.09.2026) every personal-data / personal-context /
       deletion-request call from the bot names its subject; an unnamed
       caller on this surface is therefore not a legitimate caller.
    3. **Subject.** The header is resolved WITHOUT creating a row
       (:func:`users.services.resolve_external_user_readonly`) and must
       equal the UUID in the URL.

    Why not ``has_object_permission``: the subject here is a URL kwarg, not
    a fetched object — the check must run before the view touches the row,
    and a view that 404s on a foreign subject would already have looked.
    Same technique as ``_check_user_scope`` in ``payments/views.py`` (C7.6),
    lifted into a permission so it cannot be forgotten per method.

    ``request.user`` is NOT replaced: the views on this surface resolve the
    subject from the URL themselves, and the permission's job is only to
    say whether the caller may name that subject at all.

    ``subject_of`` (DRF-1815) — what the URL names on this surface: the
    actor's own ``User`` UUID by default. A surface whose URL names the
    actor's *specialist profile* instead overrides it (see
    :class:`IsInternalBearerForSpecialistSubject`); the checks above it
    are the same, only the last comparison changes. ``None`` means the
    actor has no such subject at all — refused with its own reason, so a
    proxy that has never been linked reads as «no subject», not as «wrong
    subject».
    """

    message = "Internal service auth required"

    def subject_of(self, actor: Any) -> str | None:
        return str(actor.pk)

    def has_permission(self, request: Any, view: Any) -> bool:
        from users.services import resolve_external_user_readonly

        purpose = _credential_purpose(request)
        if purpose != _CredentialPurpose.INTERNAL:
            if purpose == _CredentialPurpose.PROVISIONING:
                logger.warning(
                    "internal.subject_authz.wrong_purpose path=%s",
                    request.path,
                )
            _publish_verdict(
                request, purpose=purpose, actor=None, actor_named=False,
                allowed=False,
                reason={
                    _CredentialPurpose.PROVISIONING: "wrong_purpose",
                    _CredentialPurpose.NONE: "no_credential",
                    _CredentialPurpose.UNKNOWN: "unknown_credential",
                }[purpose],
            )
            return False

        subject_kwarg = getattr(view, "subject_url_kwarg", None)
        if not subject_kwarg or subject_kwarg not in getattr(view, "kwargs", {}):
            # Not a caller error — ours. Fail closed and say so: a route
            # reaching this branch is a route nobody is guarding.
            logger.error(
                "internal.subject_authz.view_misconfigured path=%s view=%s "
                "subject_url_kwarg=%r",
                request.path, type(view).__name__, subject_kwarg,
            )
            _publish_verdict(
                request, purpose=purpose, actor=None, actor_named=False,
                allowed=False, reason="view_misconfigured",
            )
            return False
        subject_id = str(view.kwargs[subject_kwarg])

        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        if not external_user_id:
            logger.warning(
                "internal.subject_authz.unnamed_actor path=%s subject=%s",
                request.path, subject_id,
            )
            self.message = "X-External-User-ID is required on this surface"
            _publish_verdict(
                request, purpose=purpose, actor=None, actor_named=False,
                allowed=False, reason="unnamed_actor",
            )
            return False

        actor = resolve_external_user_readonly(external_user_id)
        if actor is None:
            # Malformed, or an identity Ayla has never seen. "Not resolved"
            # is not "resolve it for them": provisioning is a different
            # purpose with a different credential.
            logger.warning(
                "internal.subject_authz.unknown_actor path=%s subject=%s",
                request.path, subject_id,
            )
            self.message = "acting subject is unknown"
            _publish_verdict(
                request, purpose=purpose, actor=None, actor_named=True,
                allowed=False, reason="unknown_actor",
            )
            return False

        actor_subject = self.subject_of(actor)
        if actor_subject is None:
            logger.warning(
                "internal.subject_authz.subject_unresolved path=%s subject=%s actor=%s",
                request.path, subject_id, actor.pk,
            )
            self.message = "acting subject has no such profile"
            _publish_verdict(
                request, purpose=purpose, actor=actor, actor_named=True,
                allowed=False, reason="subject_unresolved",
            )
            return False

        if actor_subject != subject_id:
            # No PII: two UUIDs and a path.
            logger.warning(
                "internal.subject_authz.subject_mismatch path=%s subject=%s actor=%s",
                request.path, subject_id, actor.pk,
            )
            self.message = "path subject does not match the acting subject"
            _publish_verdict(
                request, purpose=purpose, actor=actor, actor_named=True,
                allowed=False, reason="subject_mismatch",
            )
            return False

        _publish_verdict(request, purpose=purpose, actor=actor, actor_named=True, allowed=True)
        return True


class IsInternalBearerForSpecialistSubject(IsInternalBearerForSubject):
    """Same gate, URL names the actor's ``SpecialistProfile`` (DRF-1815).

    For ``/internal/specialists/{specialist_id}/…`` writes the master makes
    about their own workspace (working hours first). The actor is resolved
    exactly as above — runtime bearer, named, followed through the LINKED
    binding — and the profile compared is the one hanging off that actor.
    A proxy that has never been linked has no profile and is refused as
    ``subject_unresolved``: until the pre-LINKED principal (M28) exists
    there is nowhere to write, and this class must not pretend otherwise.
    """

    def subject_of(self, actor: Any) -> str | None:
        profile = getattr(actor, "specialist_profile", None)
        return str(profile.pk) if profile is not None else None


def _publish_verdict(
    request: Any, *, purpose: str, actor: Any, actor_named: bool,
    allowed: bool, reason: str = "",
) -> None:
    """Hand the verdict to the audit mixin (DRF-1753, owner §96).

    The durable row is written by
    ``privacy_audit.mixins.AuditedPersonalDataAccess`` — a permission is the
    wrong place for a DB write, and more concretely: on an allowed request
    the row must be written in the same transaction as the effect, which
    only the view layer can arrange. The log lines above stay as the fast
    path for an operator; neither they nor the verdict carry the credential,
    the external identity, or any personal value.

    ``auditable`` is False exactly when there is no authenticated caller to
    describe: no bearer at all, or one we never issued. Recording a durable
    row for every anonymous knock would hand an unauthenticated caller a way
    to grow our storage (see ``privacy_audit.outcome``).
    """
    from privacy_audit.outcome import SubjectAuthzOutcome, attach

    attach(request, SubjectAuthzOutcome(
        caller_purpose=str(purpose),
        actor=actor,
        actor_named=actor_named,
        allowed=allowed,
        auditable=purpose in (_CredentialPurpose.INTERNAL, _CredentialPurpose.PROVISIONING),
        reason=reason,
    ))
