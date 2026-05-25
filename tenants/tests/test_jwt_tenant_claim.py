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
from rest_framework_simplejwt.tokens import AccessToken

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
    """#246 sub-phase 1.E — role-aware JWT tenant claim:
    customer always gets None (multi-provider model); staff gets
    primary active staff/admin TUR."""

    def test_customer_role_claim_is_none(self, tenant):
        """Per founder ack 2026-05-25: customer is multi-provider by
        default — JWT carries no primary tenant. Tenant context
        comes from X-Tenant header per-request."""
        user = User.objects.create_user(
            username="claim1", phone="+79991115001", role="client",
            tenant=tenant,  # Legacy FK — irrelevant to claim post-1.E.
        )
        result = AuthService.issue_tokens(user)
        access = AccessToken(result["access_token"])
        assert access["tenant_id"] is None

    def test_customer_without_tenant_fk_claim_is_none(self):
        user = User.objects.create_user(
            username="claim2", phone="+79991115002", role="client",
        )
        result = AuthService.issue_tokens(user)
        access = AccessToken(result["access_token"])
        assert access["tenant_id"] is None

    def test_specialist_with_staff_tur_gets_that_tenant(self, tenant):
        """Staff (specialist/admin) get their primary active staff/admin
        TUR's tenant as the JWT primary."""
        from users.models import TenantUserRelationship
        user = User.objects.create_user(
            username="claim_spec", phone="+79991115006", role="specialist",
        )
        # User.post_save bridge creates TUR with role='staff' when
        # user.tenant is set. Use direct create to pin the role.
        TenantUserRelationship.objects.filter(user=user).delete()
        TenantUserRelationship.objects.create(
            user=user, tenant=tenant,
            role=TenantUserRelationship.Role.STAFF,
        )
        result = AuthService.issue_tokens(user)
        access = AccessToken(result["access_token"])
        assert access["tenant_id"] == str(tenant.id)

    def test_specialist_with_no_staff_tur_claim_is_none(self):
        """Brand-new specialist with no TUR yet → no JWT primary.
        TenantContextMiddleware falls back to X-Tenant header."""
        from users.models import TenantUserRelationship
        user = User.objects.create_user(
            username="claim_spec_empty", phone="+79991115007",
            role="specialist",
        )
        TenantUserRelationship.objects.filter(user=user).delete()
        result = AuthService.issue_tokens(user)
        access = AccessToken(result["access_token"])
        assert access["tenant_id"] is None


# ---------------------------------------------------------------------------
# Surface 2 — refresh re-resolves tenant_id from the live row
# ---------------------------------------------------------------------------


class TestRefreshResolvesLiveTenant:
    """Post-1.E: customer refresh always re-stamps None. Staff refresh
    picks up the current primary staff TUR (admin-reassignment flow)."""

    def test_customer_refresh_stays_none(self, tenant):
        """Customer JWT is invariant on tenant claim — refresh never
        picks up any tenant. Multi-provider customers manage context
        via X-Tenant header, not JWT."""
        user = User.objects.create_user(
            username="claim_move", phone="+79991115003", role="client",
            tenant=tenant,
        )
        first = AuthService.issue_tokens(user)
        first_access = AccessToken(first["access_token"])
        assert first_access["tenant_id"] is None

        ser = TenantAwareTokenRefreshSerializer(
            data={"refresh": first["refresh_token"]},
        )
        ser.is_valid(raise_exception=True)
        new_access = AccessToken(ser.validated_data["access"])
        assert new_access["tenant_id"] is None

    def test_staff_refresh_picks_up_new_primary_tur(
        self, tenant, other_tenant,
    ):
        """Staff with TUR(tenant) gets JWT.tenant=tenant. Adding
        TUR(other_tenant) more recently makes other_tenant the new
        primary (granted_at DESC tiebreak). Refresh stamps the new
        primary into the access token."""
        from users.models import TenantUserRelationship
        user = User.objects.create_user(
            username="claim_staff_move", phone="+79991115008",
            role="specialist",
        )
        TenantUserRelationship.objects.filter(user=user).delete()
        TenantUserRelationship.objects.create(
            user=user, tenant=tenant,
            role=TenantUserRelationship.Role.STAFF,
        )

        first = AuthService.issue_tokens(user)
        first_access = AccessToken(first["access_token"])
        assert first_access["tenant_id"] == str(tenant.id)

        # Specialist mobility — new staff TUR added later (granted_at
        # higher → becomes primary).
        TenantUserRelationship.objects.create(
            user=user, tenant=other_tenant,
            role=TenantUserRelationship.Role.STAFF,
        )

        ser = TenantAwareTokenRefreshSerializer(
            data={"refresh": first["refresh_token"]},
        )
        ser.is_valid(raise_exception=True)
        new_access = AccessToken(ser.validated_data["access"])
        assert new_access["tenant_id"] == str(other_tenant.id)

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
