"""DRF-242.8 — JWT tenant_id claim + middleware fallback + OpenAPI.

Three surfaces exercised here:

1. ``AuthService.issue_tokens`` stamps ``tenant_id`` into the access token.
2. ``TenantAwareTokenRefreshSerializer`` re-resolves ``tenant_id`` from
   the live ``User`` row on every refresh, so admin moves propagate
   without re-login.
3. ``TenantContextMiddleware`` falls back to the JWT claim when the
   ``X-Tenant`` header is missing.
4. ``add_x_tenant_header`` injects the parameter on every tenant-scoped
   operation in the OpenAPI schema.
"""
from __future__ import annotations

import pytest
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from tenants.models import Tenant
from tenants.openapi import add_x_tenant_header
from users.auth_serializers import TenantAwareTokenRefreshSerializer
from users.models import User
from users.services import AuthService


pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="claimcheck", name="Claim Check")


@pytest.fixture
def other_tenant():
    return Tenant.objects.create(slug="claimcheck-2", name="Claim Check 2")


# ---------------------------------------------------------------------------
# Surface 1 — issue_tokens stamps tenant_id claim
# ---------------------------------------------------------------------------


class TestIssueTokensStampsTenantClaim:
    def test_user_with_tenant(self, tenant):
        user = User.objects.create_user(
            username="claim1", phone="+79991115001", role="client",
            tenant=tenant,
        )
        result = AuthService.issue_tokens(user)
        access = AccessToken(result["access_token"])
        assert access["tenant_id"] == str(tenant.id)

    def test_user_without_tenant(self):
        user = User.objects.create_user(
            username="claim2", phone="+79991115002", role="client",
        )
        result = AuthService.issue_tokens(user)
        access = AccessToken(result["access_token"])
        # null is the documented contract — middleware reads it as "no
        # JWT-side hint, fall through to permissive mode".
        assert access["tenant_id"] is None


# ---------------------------------------------------------------------------
# Surface 2 — refresh re-resolves tenant_id from the live row
# ---------------------------------------------------------------------------


class TestRefreshResolvesLiveTenant:
    def test_refresh_picks_up_admin_reassignment(self, tenant, other_tenant):
        """User starts on tenant A, admin moves them to tenant B, the
        next refresh stamps B into the new access token. Without this
        the user keeps a stale tenant_id for up to ACCESS_TOKEN_LIFETIME."""
        user = User.objects.create_user(
            username="claim_move", phone="+79991115003", role="client",
            tenant=tenant,
        )
        # Initial issue — tenant_id == tenant.id
        first = AuthService.issue_tokens(user)
        first_access = AccessToken(first["access_token"])
        assert first_access["tenant_id"] == str(tenant.id)

        # Admin reassigns
        user.tenant = other_tenant
        user.save(update_fields=["tenant"])

        # Refresh — must pick up the new tenant
        ser = TenantAwareTokenRefreshSerializer(
            data={"refresh": first["refresh_token"]},
        )
        ser.is_valid(raise_exception=True)
        new_access = AccessToken(ser.validated_data["access"])
        assert new_access["tenant_id"] == str(other_tenant.id)

    def test_refresh_picks_up_tenant_removal(self, tenant):
        """Admin can also drop a tenant assignment — claim turns null."""
        user = User.objects.create_user(
            username="claim_drop", phone="+79991115004", role="client",
            tenant=tenant,
        )
        first = AuthService.issue_tokens(user)

        user.tenant = None
        user.save(update_fields=["tenant"])

        ser = TenantAwareTokenRefreshSerializer(
            data={"refresh": first["refresh_token"]},
        )
        ser.is_valid(raise_exception=True)
        new_access = AccessToken(ser.validated_data["access"])
        assert new_access["tenant_id"] is None

    def test_refresh_propagates_missing_user_error(self, tenant):
        """User deleted between issue and refresh — simplejwt's base
        ``TokenRefreshSerializer`` already enforces user existence via
        ``UPDATE_LAST_LOGIN`` lookup, so our override never reaches the
        ``User.DoesNotExist`` branch. The downstream ``DoesNotExist``
        is the right behaviour: the refresh token is no longer valid
        because its subject no longer exists."""
        from django.db.utils import IntegrityError  # noqa: F401  (typing aid)

        user = User.objects.create_user(
            username="claim_gone", phone="+79991115005", role="client",
            tenant=tenant,
        )
        first = AuthService.issue_tokens(user)
        user.delete()

        ser = TenantAwareTokenRefreshSerializer(
            data={"refresh": first["refresh_token"]},
        )
        with pytest.raises(User.DoesNotExist):
            ser.is_valid(raise_exception=True)


# ---------------------------------------------------------------------------
# Surface 3 — middleware falls back to JWT claim
# ---------------------------------------------------------------------------


class TestMiddlewareJwtFallback:
    """The middleware imports settings lazily, so we need a real Django
    request flowing through. APIClient + a tenant-scoped probe endpoint
    is the cheapest setup; we use the public health endpoint as the
    canary because it's tenant-agnostic but still goes through the
    middleware chain."""

    def test_jwt_claim_resolves_tenant_when_header_missing(self, tenant, rf):
        from users.middleware import TenantContextMiddleware

        user = User.objects.create_user(
            username="mw_jwt", phone="+79991115011", role="client",
            tenant=tenant,
        )
        tokens = AuthService.issue_tokens(user)

        captured = {}

        def view(request):
            captured["tenant"] = request.tenant
            from django.http import HttpResponse
            return HttpResponse("ok")

        mw = TenantContextMiddleware(view)
        request = rf.get(
            "/api/v1/specialists/",
            HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}",
        )
        mw(request)
        assert captured["tenant"] == tenant

    def test_explicit_header_wins_over_jwt(self, tenant, other_tenant, rf):
        """Header is the caller's explicit intent; JWT is the fallback."""
        from users.middleware import TenantContextMiddleware

        user = User.objects.create_user(
            username="mw_both", phone="+79991115012", role="client",
            tenant=tenant,
        )
        tokens = AuthService.issue_tokens(user)

        captured = {}

        def view(request):
            captured["tenant"] = request.tenant
            from django.http import HttpResponse
            return HttpResponse("ok")

        mw = TenantContextMiddleware(view)
        request = rf.get(
            "/api/v1/specialists/",
            HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}",
            HTTP_X_TENANT=other_tenant.slug,
        )
        mw(request)
        assert captured["tenant"] == other_tenant

    def test_invalid_jwt_does_not_crash(self, rf):
        """Malformed bearer tokens must not raise — middleware silently
        falls through to permissive mode (tenant=None)."""
        from users.middleware import TenantContextMiddleware

        captured = {}

        def view(request):
            captured["tenant"] = request.tenant
            from django.http import HttpResponse
            return HttpResponse("ok")

        mw = TenantContextMiddleware(view)
        request = rf.get(
            "/api/v1/specialists/",
            HTTP_AUTHORIZATION="Bearer obviously-not-a-jwt",
        )
        mw(request)
        assert captured["tenant"] is None


# ---------------------------------------------------------------------------
# Surface 4 — OpenAPI X-Tenant header injection
# ---------------------------------------------------------------------------


class TestOpenApiTenantHeader:
    """The hook walks the assembled schema dict; we feed it minimal
    inputs and verify the parameter lands where expected."""

    def test_hook_adds_header_to_tenant_scoped_paths(self):
        schema = {
            "paths": {
                "/api/v1/specialists/": {
                    "get": {"operationId": "specialists_list"},
                },
                "/api/v1/appointments/": {
                    "get": {"operationId": "appointments_list"},
                    "post": {"operationId": "appointments_create"},
                },
            },
        }
        result = add_x_tenant_header(schema, generator=None, request=None, public=True)
        for path in ("/api/v1/specialists/", "/api/v1/appointments/"):
            for method, op in result["paths"][path].items():
                params = op["parameters"]
                names = [p["name"] for p in params]
                assert "X-Tenant" in names, (
                    f"X-Tenant missing on {method.upper()} {path}"
                )

    def test_hook_skips_auth_paths(self):
        schema = {
            "paths": {
                "/api/v1/auth/login/": {
                    "post": {"operationId": "login"},
                },
                "/api/v1/health/": {
                    "get": {"operationId": "health"},
                },
                "/api/v1/nutrition/internal/scan/": {
                    "post": {"operationId": "internal_scan"},
                },
            },
        }
        result = add_x_tenant_header(schema, generator=None, request=None, public=True)
        for path in result["paths"].values():
            for op in path.values():
                params = op.get("parameters", [])
                names = [p["name"] for p in params]
                assert "X-Tenant" not in names, (
                    "X-Tenant must not appear on auth/health/internal paths"
                )

    def test_hook_is_idempotent(self):
        """A view explicitly declaring X-Tenant via @extend_schema must
        not get a duplicate when the hook runs."""
        existing = {
            "name": "X-Tenant",
            "in": "header",
            "schema": {"type": "string"},
        }
        schema = {
            "paths": {
                "/api/v1/specialists/": {
                    "get": {
                        "operationId": "list",
                        "parameters": [existing],
                    },
                },
            },
        }
        result = add_x_tenant_header(schema, generator=None, request=None, public=True)
        params = result["paths"]["/api/v1/specialists/"]["get"]["parameters"]
        names = [p["name"] for p in params]
        assert names.count("X-Tenant") == 1
