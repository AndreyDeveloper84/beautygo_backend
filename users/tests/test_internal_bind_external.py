"""Tests for POST /api/v1/internal/users/bind-external/ — E2E-BOT-02B.

The endpoint is the write half of the cross-repo identity binding.
PROVISIONING-ONLY (security review P1-1): it is gated by a dedicated
credential (``AYLA_IDENTITY_PROVISIONING_TOKEN``) that the standard BOT
runtime never holds — production bot-driven binding is not supported
until a verified ownership flow exists.

Test surface:

* Bearer auth — missing / wrong token → 403; the GENERAL bot service
  token (AYLA_INTERNAL_API_TOKEN) is also 403 by construction.
* Happy path — 200, proxy created + linked, resolver follows the link.
* Idempotency — re-binding the same pair → 200, no duplicate proxy.
* Validation — malformed external_user_id → 400.
* Unknown / unbindable ayla_user_id → 404 (info-hidden).
* Rebind to a DIFFERENT account → 409, original binding untouched.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from users.models import User
from users.services import resolve_external_user


URL = "/api/v1/internal/users/bind-external/"


@pytest.fixture
def bearer_token(settings):
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = "test-provisioning-bind"
    return "test-provisioning-bind"


@pytest.fixture
def api(bearer_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer_token}")
    return client


@pytest.fixture
def real_customer(db):
    return User.objects.create_user(
        username="bind-real-customer", password="x", role="client",
        phone="+79995550001", is_proxy=False, is_verified=True,
    )


def _payload(real_customer, external_user_id="bot:max:e2e"):
    return {
        "external_user_id": external_user_id,
        "ayla_user_id": str(real_customer.pk),
    }


@pytest.mark.django_db
class TestBindExternalAuth:
    def test_missing_bearer_denied(self, real_customer):
        client = APIClient()
        r = client.post(URL, _payload(real_customer), format="json")
        assert r.status_code == 403

    def test_wrong_bearer_denied(self, api):
        api.credentials(HTTP_AUTHORIZATION="Bearer wrong")
        r = api.post(URL, {"external_user_id": "bot:max:x",
                           "ayla_user_id": str(uuid4())}, format="json")
        assert r.status_code == 403

    def test_general_bot_service_token_denied(self, api, settings,
                                              real_customer):
        """P1-1 proof: the standard BOT runtime credential — valid for
        every other internal s2s surface — CANNOT call the binding
        endpoint. Only the dedicated provisioning token passes."""
        settings.AYLA_INTERNAL_API_TOKEN = "test-general-bot-token"
        api.credentials(HTTP_AUTHORIZATION="Bearer test-general-bot-token")
        r = api.post(URL, _payload(real_customer), format="json")
        assert r.status_code == 403
        # …and the identity stays unbound (no side effect from the
        # rejected attempt beyond the 403 itself).
        assert resolve_external_user("bot:max:e2e").linked_user_id is None

    def test_empty_provisioning_token_fails_closed(self, api, settings,
                                                   real_customer):
        """Unprovisioned deployment: empty setting disables the
        endpoint entirely — even a caller presenting the empty string
        gets 403."""
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = ""
        r = api.post(URL, _payload(real_customer), format="json")
        assert r.status_code == 403

    def test_equal_tokens_fail_closed(self, api, settings, real_customer):
        """Misconfiguration hard-fail: if ops sets the provisioning
        token EQUAL to the general bot token, the boundary must not
        depend on ops discipline — every request is denied."""
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = "shared-value"
        settings.AYLA_INTERNAL_API_TOKEN = "shared-value"
        api.credentials(HTTP_AUTHORIZATION="Bearer shared-value")
        r = api.post(URL, _payload(real_customer), format="json")
        assert r.status_code == 403
        assert resolve_external_user("bot:max:e2e").linked_user_id is None

    def test_equal_tokens_system_check_fails(self, settings):
        """The same misconfiguration is loud at boot, not just closed
        at runtime: system check users.E001 reports it."""
        from users.checks import identity_provisioning_token_check
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = "shared-value"
        settings.AYLA_INTERNAL_API_TOKEN = "shared-value"
        errors = identity_provisioning_token_check(None)
        assert [e.id for e in errors] == ["users.E001"]
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = "distinct-value"
        assert identity_provisioning_token_check(None) == []


@pytest.mark.django_db
class TestBindExternalContract:
    def test_happy_path_binds_and_resolver_follows(self, api, real_customer):
        r = api.post(URL, _payload(real_customer), format="json")
        assert r.status_code == 200
        body = r.json()["data"]
        assert body["bound"] is True
        assert body["proxy_created"] is True
        assert body["ayla_user_id"] == str(real_customer.pk)

        resolved = resolve_external_user("bot:max:e2e")
        assert resolved.pk == real_customer.pk

    def test_rebind_same_pair_is_idempotent(self, api, real_customer):
        first = api.post(URL, _payload(real_customer), format="json")
        second = api.post(URL, _payload(real_customer), format="json")
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["data"]["proxy_created"] is False
        assert (
            first.json()["data"]["proxy_user_id"]
            == second.json()["data"]["proxy_user_id"]
        )

    def test_malformed_external_id_rejected(self, api, real_customer):
        r = api.post(URL, _payload(real_customer, "not-an-external-id"),
                     format="json")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_ayla_user_rejected(self, api):
        r = api.post(URL, {"external_user_id": "bot:max:ghost",
                           "ayla_user_id": str(uuid4())}, format="json")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_proxy_target_rejected_as_unbindable(self, api):
        proxy = resolve_external_user("bot:max:proxy-target")
        r = api.post(URL, {"external_user_id": "bot:max:chain",
                           "ayla_user_id": str(proxy.pk)}, format="json")
        assert r.status_code == 404

    def test_rebind_different_account_conflicts(self, api, real_customer):
        other = User.objects.create_user(
            username="bind-other-customer", password="x", role="client",
            phone="+79995550002", is_proxy=False,
        )
        assert api.post(URL, _payload(real_customer), format="json").status_code == 200
        r = api.post(URL, _payload(other), format="json")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "CONFLICT"
        # Original binding is authoritative after the failed rebind.
        assert resolve_external_user("bot:max:e2e").pk == real_customer.pk

    def test_missing_fields_rejected(self, api):
        r = api.post(URL, {}, format="json")
        assert r.status_code == 400


@pytest.mark.django_db
class TestBindExternalTrustBoundary:
    """Security review P1-2: the caller names the target account, so
    the contract is narrowed server-side — only real CLIENT accounts
    are bindable, and rejections are info-hidden (same 404 body as an
    unknown id)."""

    def test_specialist_target_rejected_info_hidden(self, api):
        specialist = User.objects.create_user(
            username="bind-target-spec", password="x", role="specialist",
            phone="+79995550010", is_proxy=False,
        )
        r = api.post(URL, {"external_user_id": "bot:max:to-spec",
                           "ayla_user_id": str(specialist.pk)}, format="json")
        assert r.status_code == 404
        # Same envelope as the unknown-id case — no oracle for
        # "this UUID exists but is a staff account".
        unknown = api.post(URL, {"external_user_id": "bot:max:to-ghost",
                                 "ayla_user_id": str(uuid4())}, format="json")
        assert r.json()["error"]["code"] == unknown.json()["error"]["code"]

    def test_admin_target_rejected(self, api):
        admin = User.objects.create_user(
            username="bind-target-admin", password="x", role="admin",
            phone="+79995550011", is_proxy=False,
        )
        r = api.post(URL, {"external_user_id": "bot:max:to-admin",
                           "ayla_user_id": str(admin.pk)}, format="json")
        assert r.status_code == 404


@pytest.mark.django_db
class TestBindExternalAudit:
    """Security review P1-3: the endpoint leaves a durable audit row."""

    def test_happy_path_writes_audit_event(self, api, real_customer):
        r = api.post(URL, _payload(real_customer, "bot:max:audit-api"),
                     format="json")
        assert r.status_code == 200
        from analytics import event_catalogue
        from analytics.models import AnalyticsEvent
        (event,) = AnalyticsEvent.objects.filter(
            event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
            payload__external_user_id="bot:max:audit-api",
        )
        assert event.payload["result"] == "created"
        assert event.payload["initiator"] == "identity_provisioning"
        assert event.actor_id == real_customer.pk

    def test_conflict_writes_audit_event(self, api, real_customer):
        api.post(URL, _payload(real_customer, "bot:max:audit-cf"),
                 format="json")
        other = User.objects.create_user(
            username="bind-audit-other", password="x", role="client",
            phone="+79995550012", is_proxy=False,
        )
        r = api.post(URL, _payload(other, "bot:max:audit-cf"), format="json")
        assert r.status_code == 409
        from analytics import event_catalogue
        from analytics.models import AnalyticsEvent
        events = AnalyticsEvent.objects.filter(
            event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
            payload__external_user_id="bot:max:audit-cf",
        ).order_by("created_at")
        assert [e.payload["result"] for e in events] == ["created", "conflict"]
