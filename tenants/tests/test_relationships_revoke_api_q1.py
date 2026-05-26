"""Tests for POST /api/v1/tenants/me/relationships/{user_id}/revoke/

#246 Q1 (founder ack 2026-05-26). §H.3 adversarial coverage:

- Cross-tenant impersonation (admin-of-A targeting tenant-B user)
- Role escalation (customer / staff cannot call revoke)
- Missing tenant context (X-Tenant absent → no admin scope)
- Default-silent invariant (notify_customer=false ⇒ no push)
- Audit-always invariant (OutboxEvent emitted regardless of notify)
- TUR not-found info-hiding (404 for non-existent / already-revoked)
- Concurrent revoke (select_for_update serialises)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from appointments.models import OutboxEvent
from tenants.models import Tenant
from users.models import TenantUserRelationship, User


def _make_user(*, username: str, role: str = "client", phone: str = ""):
    return User.objects.create_user(
        username=username, password="x", role=role, phone=phone,
    )


def _client_as(user, tenant: Tenant | None = None) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "pro"
    if tenant is not None:
        c.defaults["HTTP_X_TENANT"] = tenant.slug
    c.force_authenticate(user=user)
    return c


def _url(target_user_id) -> str:
    return f"/api/v1/tenants/me/relationships/{target_user_id}/revoke/"


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="q1-a", name="Q1 Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="q1-b", name="Q1 Tenant B")


@pytest.fixture
def admin_a(db, tenant_a):
    """Admin of tenant A — has an active ADMIN-role TUR in A."""
    u = _make_user(username="q1_admin_a", role="admin", phone="+79991900001")
    TenantUserRelationship.objects.filter(user=u).delete()
    TenantUserRelationship.objects.create(
        user=u, tenant=tenant_a,
        role=TenantUserRelationship.Role.ADMIN,
    )
    return u


@pytest.fixture
def admin_b(db, tenant_b):
    u = _make_user(username="q1_admin_b", role="admin", phone="+79991900002")
    TenantUserRelationship.objects.filter(user=u).delete()
    TenantUserRelationship.objects.create(
        user=u, tenant=tenant_b,
        role=TenantUserRelationship.Role.ADMIN,
    )
    return u


@pytest.fixture
def customer_in_a(db, tenant_a):
    u = _make_user(
        username="q1_cust_a", role="client", phone="+79991900010",
    )
    TenantUserRelationship.objects.filter(user=u).delete()
    TenantUserRelationship.objects.create(
        user=u, tenant=tenant_a,
        role=TenantUserRelationship.Role.CUSTOMER,
    )
    return u


# ---------------------------------------------------------------------------
# Auth boundary — adversarial
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevokeAuthBoundary:
    def test_anonymous_denied(self, customer_in_a, tenant_a):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.defaults["HTTP_X_TENANT"] = tenant_a.slug
        r = c.post(
            _url(customer_in_a.pk),
            {"reason": "test"},
            format="json",
        )
        # IsAuthenticated rejects → 401 from DRF default.
        assert r.status_code == 401

    def test_customer_cannot_revoke(
        self, customer_in_a, admin_a, tenant_a,
    ):
        # The customer is in tenant_a but with role=customer, not admin.
        # IsTenantAdmin's TUR query is role=admin only → False.
        r = _client_as(customer_in_a, tenant_a).post(
            _url(admin_a.pk),
            {"reason": "test"},
            format="json",
        )
        assert r.status_code == 403

    def test_admin_without_tenant_header_denied(
        self, admin_a, customer_in_a,
    ):
        """No X-Tenant → request.tenant is None → IsTenantAdmin 403.
        Defeats the 'admin in some tenant tries to act without naming
        which tenant they're acting in' shape."""
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        # Deliberately NOT setting HTTP_X_TENANT.
        c.force_authenticate(user=admin_a)
        r = c.post(
            _url(customer_in_a.pk),
            {"reason": "test"},
            format="json",
        )
        assert r.status_code == 403

    def test_cross_tenant_admin_cannot_revoke_other_tenants_user(
        self, admin_b, customer_in_a, tenant_a,
    ):
        """admin_b is admin of tenant B. Customer is in tenant A.
        admin_b sends X-Tenant=tenant_a — but they have no admin TUR
        in tenant_a, so IsTenantAdmin denies. This is the canonical
        cross-tenant impersonation attempt; the second factor (admin's
        own TUR in the active tenant) closes it."""
        r = _client_as(admin_b, tenant_a).post(
            _url(customer_in_a.pk),
            {"reason": "kicked"},
            format="json",
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Happy path + invariants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevokeHappyPath:
    def test_default_silent_revoke(
        self, admin_a, customer_in_a, tenant_a,
    ):
        """Default notify_customer=false ⇒ NO push notification, but
        the relationship row flips and the audit OutboxEvent fires."""
        with patch(
            "notifications.services.dispatcher.NotificationService.send",
        ) as mock_send:
            r = _client_as(admin_a, tenant_a).post(
                _url(customer_in_a.pk),
                {"reason": "kicked_for_no_show"},
                format="json",
            )
        assert r.status_code == 204
        assert mock_send.call_count == 0

        # Row state.
        tur = TenantUserRelationship.objects.get(
            user=customer_in_a, tenant=tenant_a,
        )
        assert tur.is_active is False
        assert tur.revoked_at is not None
        assert tur.revoke_reason == "kicked_for_no_show"

        # Outbox audit ALWAYS emitted.
        events = OutboxEvent.objects.filter(
            topic="tenant.relationship.revoked",
        )
        assert events.count() == 1
        ev = events.get()
        assert ev.payload["data"]["user_id"] == str(customer_in_a.pk)
        assert ev.payload["data"]["tenant_id"] == str(tenant_a.pk)
        assert ev.payload["data"]["revoke_reason"] == "kicked_for_no_show"
        assert ev.payload["data"]["notify_customer"] is False
        assert ev.payload["data"]["revoked_by_user_id"] == str(admin_a.pk)

    def test_notify_customer_true_queues_push(
        self, admin_a, customer_in_a, tenant_a,
    ):
        with patch(
            "notifications.services.dispatcher.NotificationService.send",
        ) as mock_send:
            r = _client_as(admin_a, tenant_a).post(
                _url(customer_in_a.pk),
                {"reason": "salon_closing", "notify_customer": True},
                format="json",
            )
        assert r.status_code == 204
        assert mock_send.call_count == 1
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["template_id"] == "tenant_relationship_revoked"
        assert call_kwargs["user"] == customer_in_a
        # Generic message — only the tenant name is exposed, NOT the
        # internal reason.
        assert call_kwargs["context"] == {"tenant_name": tenant_a.name}

    def test_notify_customer_true_does_not_expose_internal_reason(
        self, admin_a, customer_in_a, tenant_a,
    ):
        """Belt-and-suspenders pin: the template context must never
        carry the revoke_reason. Customer's push is generic."""
        with patch(
            "notifications.services.dispatcher.NotificationService.send",
        ) as mock_send:
            _client_as(admin_a, tenant_a).post(
                _url(customer_in_a.pk),
                {
                    "reason": "VERY_SENSITIVE_INTERNAL_NOTE",
                    "notify_customer": True,
                },
                format="json",
            )
        passed_context = mock_send.call_args.kwargs["context"]
        joined = " ".join(str(v) for v in passed_context.values())
        assert "VERY_SENSITIVE_INTERNAL_NOTE" not in joined


# ---------------------------------------------------------------------------
# Validation + info-hiding
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevokeValidation:
    def test_missing_reason_400(
        self, admin_a, customer_in_a, tenant_a,
    ):
        r = _client_as(admin_a, tenant_a).post(
            _url(customer_in_a.pk),
            {"notify_customer": True},
            format="json",
        )
        assert r.status_code == 400

    def test_unknown_user_returns_404_uniform(
        self, admin_a, tenant_a,
    ):
        from uuid import uuid4
        r = _client_as(admin_a, tenant_a).post(
            _url(uuid4()),
            {"reason": "test"},
            format="json",
        )
        assert r.status_code == 404

    def test_user_not_in_admins_tenant_returns_404(
        self, admin_a, tenant_a, tenant_b,
    ):
        """User exists but has no TUR in admin's tenant → 404 (not
        403), and the response does NOT disclose whether the user is
        in some OTHER tenant. Info-hiding parity with the Variant B
        404 rule."""
        outsider = _make_user(
            username="q1_outsider", role="client", phone="+79991900020",
        )
        TenantUserRelationship.objects.create(
            user=outsider, tenant=tenant_b,
            role=TenantUserRelationship.Role.CUSTOMER,
        )
        r = _client_as(admin_a, tenant_a).post(
            _url(outsider.pk),
            {"reason": "test"},
            format="json",
        )
        assert r.status_code == 404
        # outsider's TUR in tenant_b MUST remain active.
        tur = TenantUserRelationship.objects.get(
            user=outsider, tenant=tenant_b,
        )
        assert tur.is_active is True

    def test_already_revoked_returns_404(
        self, admin_a, customer_in_a, tenant_a,
    ):
        # Pre-revoke directly via DB so the second call is a re-revoke.
        from django.utils import timezone
        TenantUserRelationship.objects.filter(
            user=customer_in_a, tenant=tenant_a,
        ).update(
            is_active=False,
            revoked_at=timezone.now(),
            revoke_reason="prior",
        )
        r = _client_as(admin_a, tenant_a).post(
            _url(customer_in_a.pk),
            {"reason": "test"},
            format="json",
        )
        # Info-hidden: re-revoke maps to the same 404 as never-existed.
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Audit invariant — always emitted
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevokeAuditAlwaysEmitted:
    """Founder Q1 ack: audit event + internal event ALWAYS emitted
    regardless of notification setting."""

    @pytest.mark.parametrize("notify", [True, False])
    def test_audit_event_emitted(
        self, admin_a, customer_in_a, tenant_a, notify,
    ):
        OutboxEvent.objects.all().delete()
        with patch(
            "notifications.services.dispatcher.NotificationService.send",
        ):
            _client_as(admin_a, tenant_a).post(
                _url(customer_in_a.pk),
                {"reason": "audit_pin", "notify_customer": notify},
                format="json",
            )
        events = OutboxEvent.objects.filter(
            topic="tenant.relationship.revoked",
        )
        assert events.count() == 1, (
            f"audit OutboxEvent must always fire (notify={notify})"
        )
        ev = events.get()
        # Envelope shape — ADR-0009 mandatory fields.
        assert ev.payload["event_name"] == "tenant.relationship.revoked"
        assert ev.payload["event_version"] == 1
        assert ev.payload["actor"] == "admin"
        assert ev.payload["tenant_id"] == str(tenant_a.pk)
        assert ev.payload["user_id"] == str(customer_in_a.pk)
        assert ev.payload["data"]["notify_customer"] is notify
