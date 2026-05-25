"""Tests for TenantContextMiddleware + IsTenantMember (DRF-242.4)."""
from __future__ import annotations

import pytest
from django.test import RequestFactory

from tenants.models import Tenant
from users.middleware import TenantContextMiddleware
from users.permissions import IsTenantMember


pytestmark = pytest.mark.django_db


def _call(request, response_func=None):
    """Drive the middleware over a request with a no-op downstream view."""
    response_func = response_func or (lambda r: ("ok", r.tenant))
    mw = TenantContextMiddleware(get_response=response_func)
    return mw(request)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TestTenantContextMiddleware:
    def test_no_header_leaves_tenant_none(self):
        request = RequestFactory().get("/api/v1/specialists/")
        _call(request)
        assert request.tenant is None

    def test_known_slug_resolves_to_tenant(self):
        t = Tenant.objects.create(slug="formula", name="Формула тела")
        request = RequestFactory().get(
            "/api/v1/specialists/", HTTP_X_TENANT="formula",
        )
        _call(request)
        assert request.tenant is not None
        assert request.tenant.id == t.id

    def test_unknown_slug_falls_back_to_none(self):
        request = RequestFactory().get(
            "/api/v1/specialists/", HTTP_X_TENANT="ghost",
        )
        _call(request)
        assert request.tenant is None

    def test_inactive_tenant_treated_as_unknown(self):
        Tenant.objects.create(slug="dead", name="D", is_active=False)
        request = RequestFactory().get(
            "/api/v1/specialists/", HTTP_X_TENANT="dead",
        )
        _call(request)
        assert request.tenant is None

    def test_excluded_path_skips_resolution(self):
        Tenant.objects.create(slug="formula", name="X")
        # Even with valid header, /admin path must skip resolution to keep
        # admin sessions tenant-agnostic.
        request = RequestFactory().get(
            "/admin/login/", HTTP_X_TENANT="formula",
        )
        _call(request)
        assert request.tenant is None

    def test_internal_nutrition_path_excluded(self):
        Tenant.objects.create(slug="formula", name="X")
        request = RequestFactory().get(
            "/api/v1/nutrition/internal/scan/", HTTP_X_TENANT="formula",
        )
        _call(request)
        assert request.tenant is None

    def test_empty_header_treated_as_missing(self):
        request = RequestFactory().get(
            "/api/v1/specialists/", HTTP_X_TENANT="",
        )
        _call(request)
        assert request.tenant is None


# ---------------------------------------------------------------------------
# Strict-mode middleware (DRF-242.5)
# ---------------------------------------------------------------------------


class TestTenantContextMiddlewareStrictMode:
    def test_strict_mode_missing_header_returns_400(self, settings):
        settings.MULTI_TENANT_STRICT = True
        request = RequestFactory().get("/api/v1/specialists/")
        # Downstream view should NEVER be called — middleware short-circuits.
        downstream_called = []

        def downstream(r):
            downstream_called.append(True)
            return ("ok", None)
        response = _call(request, response_func=downstream)
        assert downstream_called == []
        assert response.status_code == 400
        body = response.content.decode()
        assert "TENANT_REQUIRED" in body

    def test_strict_mode_known_header_passes_through(self, settings):
        settings.MULTI_TENANT_STRICT = True
        Tenant.objects.create(slug="formula", name="F")
        request = RequestFactory().get(
            "/api/v1/specialists/", HTTP_X_TENANT="formula",
        )
        response = _call(request)
        assert request.tenant is not None
        # _call returns the downstream tuple — strict didn't 400.
        assert response[0] == "ok"

    def test_strict_mode_unknown_slug_returns_400(self, settings):
        settings.MULTI_TENANT_STRICT = True
        request = RequestFactory().get(
            "/api/v1/specialists/", HTTP_X_TENANT="ghost",
        )
        response = _call(request)
        assert response.status_code == 400

    def test_strict_mode_excluded_path_passes_through(self, settings):
        settings.MULTI_TENANT_STRICT = True
        # /api/v1/health/ is in EXCLUDED_PATH_PREFIXES — strict shouldn't 400.
        request = RequestFactory().get("/api/v1/health/")
        response = _call(request)
        assert response[0] == "ok"

    def test_strict_mode_auth_path_opt_out(self, settings):
        settings.MULTI_TENANT_STRICT = True
        # /api/v1/auth/* is the registration handshake — pre-tenant. Must
        # pass through without a header so mobile clients can register.
        request = RequestFactory().post("/api/v1/auth/login/")
        response = _call(request)
        assert response[0] == "ok"

    def test_strict_mode_off_missing_header_passes(self, settings):
        settings.MULTI_TENANT_STRICT = False
        request = RequestFactory().get("/api/v1/specialists/")
        response = _call(request)
        assert response[0] == "ok"


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


class _StubRequest:
    def __init__(self, *, tenant=None, user=None):
        self.tenant = tenant
        self.user = user


class _AnonStub:
    """Anonymous user — no DB row needed."""
    is_authenticated = False


@pytest.mark.django_db
class TestIsTenantMember:
    """#246 sub-phase 1.B: membership reads from TenantUserRelationship.
    Previous stub-based tests don't apply — permission now hits the
    DB via `TUR.objects.filter(...).exists()`."""

    def test_anonymous_rejected(self):
        request = _StubRequest(user=_AnonStub())
        assert IsTenantMember().has_permission(request, view=None) is False

    def test_no_request_tenant_passes(self):
        """Permissive when X-Tenant header absent — global endpoints
        (profile, AI memory, marketplace) work without tenant scope."""
        from users.models import User
        user = User.objects.create_user(
            username="itm_global", password="x", role="client",
            phone="+79991111000",
        )
        request = _StubRequest(tenant=None, user=user)
        assert IsTenantMember().has_permission(request, view=None) is True

    def test_request_tenant_with_no_tur_fails(self):
        """User has no TUR for the requested tenant — reject."""
        from users.models import User
        t = Tenant.objects.create(slug="itm-t1", name="T1")
        user = User.objects.create_user(
            username="itm_no_tur", password="x", role="client",
            phone="+79991111001",
        )
        # User.post_save bridge would create a TUR if user.tenant_id
        # were set; this user has tenant_id=None so no bridge fires.
        from users.models import TenantUserRelationship
        TenantUserRelationship.objects.filter(user=user).delete()

        request = _StubRequest(tenant=t, user=user)
        assert IsTenantMember().has_permission(request, view=None) is False

    def test_active_tur_passes(self):
        """User has an active TUR for the requested tenant — allow."""
        from users.models import TenantUserRelationship, User
        t = Tenant.objects.create(slug="itm-t2", name="T2")
        user = User.objects.create_user(
            username="itm_active", password="x", role="client",
            phone="+79991111002",
        )
        TenantUserRelationship.objects.create(user=user, tenant=t)
        request = _StubRequest(tenant=t, user=user)
        assert IsTenantMember().has_permission(request, view=None) is True

    def test_revoked_tur_fails(self):
        """Revoked TUR does NOT grant access — must be is_active=True."""
        from django.utils import timezone
        from users.models import TenantUserRelationship, User
        t = Tenant.objects.create(slug="itm-t3", name="T3")
        user = User.objects.create_user(
            username="itm_revoked", password="x", role="client",
            phone="+79991111003",
        )
        TenantUserRelationship.objects.create(
            user=user, tenant=t,
            is_active=False,
            revoked_at=timezone.now(),
            revoke_reason="test_revoke",
        )
        request = _StubRequest(tenant=t, user=user)
        assert IsTenantMember().has_permission(request, view=None) is False

    def test_tur_for_different_tenant_fails(self):
        """User has TUR for tenant A, request asks for tenant B — reject.
        Customer multi-provider model: membership in one tenant doesn't
        imply access to another."""
        from users.models import TenantUserRelationship, User
        t1 = Tenant.objects.create(slug="itm-t4a", name="T4a")
        t2 = Tenant.objects.create(slug="itm-t4b", name="T4b")
        user = User.objects.create_user(
            username="itm_xtenant", password="x", role="client",
            phone="+79991111004",
        )
        TenantUserRelationship.objects.create(user=user, tenant=t1)
        request = _StubRequest(tenant=t2, user=user)
        assert IsTenantMember().has_permission(request, view=None) is False

    def test_multi_provider_customer_multiple_active_tur(self):
        """Customer with N active TUR rows — each tenant context grants
        access. Confirms the multi-provider model from #246 design doc."""
        from users.models import TenantUserRelationship, User
        t1 = Tenant.objects.create(slug="itm-t5a", name="T5a")
        t2 = Tenant.objects.create(slug="itm-t5b", name="T5b")
        user = User.objects.create_user(
            username="itm_multi", password="x", role="client",
            phone="+79991111005",
        )
        TenantUserRelationship.objects.create(user=user, tenant=t1)
        TenantUserRelationship.objects.create(user=user, tenant=t2)

        # Request for T1 → allow.
        request1 = _StubRequest(tenant=t1, user=user)
        assert IsTenantMember().has_permission(request1, view=None) is True
        # Request for T2 → also allow.
        request2 = _StubRequest(tenant=t2, user=user)
        assert IsTenantMember().has_permission(request2, view=None) is True


@pytest.mark.django_db
class TestUserPostSaveBridge:
    """#246 sub-phase 1.B: User.post_save auto-grants TUR when
    `user.tenant_id` is set. Backwards-compat for legacy
    `user.tenant=X; user.save()` callsites."""

    def test_setting_user_tenant_creates_tur(self):
        from users.models import TenantUserRelationship, User
        t = Tenant.objects.create(slug="bridge-t1", name="Bridge1")
        user = User.objects.create_user(
            username="bridge_user", password="x", role="client",
            phone="+79991122000",
        )
        # Before — no TUR.
        assert not TenantUserRelationship.objects.filter(
            user=user, tenant=t,
        ).exists()

        user.tenant = t
        user.save()

        # After — exactly one active TUR exists for (user, t).
        assert TenantUserRelationship.objects.filter(
            user=user, tenant=t, is_active=True,
        ).count() == 1

    def test_user_role_maps_to_tur_role(self):
        """client → customer, specialist → staff, admin → admin."""
        from users.models import TenantUserRelationship, User
        t = Tenant.objects.create(slug="bridge-roles", name="Roles")
        spec = User.objects.create_user(
            username="bridge_spec", password="x", role="specialist",
            phone="+79991122001",
        )
        spec.tenant = t
        spec.save()
        tur = TenantUserRelationship.objects.get(user=spec, tenant=t)
        assert tur.role == TenantUserRelationship.Role.STAFF

    def test_setting_tenant_none_does_not_revoke(self):
        """Clearing user.tenant is NOT a revoke. Existing TURs stay
        active — revoke is an explicit action."""
        from users.models import TenantUserRelationship, User
        t = Tenant.objects.create(slug="bridge-keep", name="Keep")
        user = User.objects.create_user(
            username="bridge_keep_user", password="x", role="client",
            phone="+79991122002",
        )
        user.tenant = t
        user.save()
        assert TenantUserRelationship.objects.filter(
            user=user, tenant=t, is_active=True,
        ).exists()

        user.tenant = None
        user.save()
        # Old TUR still active.
        assert TenantUserRelationship.objects.filter(
            user=user, tenant=t, is_active=True,
        ).exists()

    def test_save_idempotent_no_duplicate_tur(self):
        """Repeated saves don't create duplicate TUR rows — partial
        unique constraint enforced, get_or_create handles it."""
        from users.models import TenantUserRelationship, User
        t = Tenant.objects.create(slug="bridge-idem", name="Idem")
        user = User.objects.create_user(
            username="bridge_idem", password="x", role="client",
            phone="+79991122003",
        )
        user.tenant = t
        user.save()
        user.save()  # Second save — no error, no dup.
        user.save()  # Third — same.
        assert TenantUserRelationship.objects.filter(
            user=user, tenant=t,
        ).count() == 1
