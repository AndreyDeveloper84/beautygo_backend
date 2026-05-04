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


class _StubUser:
    def __init__(self, *, tenant_id=None, authenticated=True):
        self.is_authenticated = authenticated
        self.tenant_id = tenant_id


class _StubRequest:
    def __init__(self, *, tenant=None, user=None):
        self.tenant = tenant
        self.user = user


class TestIsTenantMember:
    def test_anonymous_rejected(self):
        request = _StubRequest(user=_StubUser(authenticated=False))
        assert IsTenantMember().has_permission(request, view=None) is False

    def test_no_request_tenant_passes(self):
        # Permissive rollout phase: missing X-Tenant header doesn't block.
        user = _StubUser(tenant_id=None)
        request = _StubRequest(tenant=None, user=user)
        assert IsTenantMember().has_permission(request, view=None) is True

    def test_request_tenant_with_no_user_tenant_fails(self):
        t = Tenant.objects.create(slug="t1", name="T1")
        request = _StubRequest(tenant=t, user=_StubUser(tenant_id=None))
        assert IsTenantMember().has_permission(request, view=None) is False

    def test_matching_tenant_passes(self):
        t = Tenant.objects.create(slug="t1", name="T1")
        request = _StubRequest(tenant=t, user=_StubUser(tenant_id=t.id))
        assert IsTenantMember().has_permission(request, view=None) is True

    def test_mismatched_tenant_fails(self):
        t1 = Tenant.objects.create(slug="t1", name="T1")
        t2 = Tenant.objects.create(slug="t2", name="T2")
        request = _StubRequest(tenant=t1, user=_StubUser(tenant_id=t2.id))
        assert IsTenantMember().has_permission(request, view=None) is False
