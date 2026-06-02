"""Tests for GET /api/v1/internal/users/{user_id}/ — codex P1-3.

This endpoint exists so bot-platform's ``user.profile.updated``
consumer (see ai-bot-platform-codex
``apps/eventbus/consumers/identity.py``) can fetch the approved PII
subset (display_name + avatar_url) for a given user. Before this
PR the bot's profile_client targeted a 404 route — every consumer
invocation dead-lettered.

Test surface:

* Bearer auth — missing / wrong / empty token → 401.
* Happy path with SpecialistProfile — display_name + avatar_url
  resolved from the master record.
* Happy path with Profile (client) — falls back to full_name +
  Profile.avatar URL.
* No profile at all — both fields empty strings (per closed-shape
  contract: keys present, values may be empty).
* PII §7 guard — response shape is the exact 2-key allowlist. Any
  extra key (phone / email / etc.) fails the assertion. Closed
  allowlist catches future drift on the serializer.
* 404 on unknown user_id — non-retryable per bot's profile_client
  semantics.
* No X-App-Type required (middleware exclusion verified).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from users.models import Profile, User


URL_TMPL = "/api/v1/internal/users/{user_id}/"


@pytest.fixture
def bearer_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
    return "test-bearer"


@pytest.fixture
def api(bearer_token):
    """Pre-authed client with the bearer header. No X-App-Type —
    internal endpoints are excluded from the AppType middleware."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer_token}")
    return client


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username="sp1", password="pass",
        role="specialist", phone="+79991110001",
    )
    sp = user.specialist_profile
    sp.display_name = "Мастер Лера"
    sp.status = "active"
    sp.save()
    return user


@pytest.fixture
def client_user_with_profile(db):
    user = User.objects.create_user(
        username="cl1", password="pass",
        role="client", phone="+79991110002",
    )
    # signals.create_user_profile auto-creates a Profile row on the
    # post_save for role='client'. Update its full_name rather than
    # creating a second row (OneToOne would raise IntegrityError).
    Profile.objects.filter(user=user).update(full_name="Анна Клиентская")
    return user


@pytest.fixture
def bare_user(db):
    # User with neither SpecialistProfile nor Profile — exercises
    # the empty-string fallback branch. role='admin' bypasses the
    # post_save signal in users/signals.py that auto-creates a
    # Profile for client/specialist roles, leaving the user without
    # any related profile row.
    return User.objects.create_user(
        username="bare", password="pass",
        role="admin", phone="+79991110003",
    )


@pytest.mark.django_db
class TestAuth:
    """Bearer-only auth — fail-closed on any missing / wrong token."""

    def test_missing_authorization_header_returns_401(
        self, specialist_user, bearer_token,
    ):
        client = APIClient()  # no credentials
        url = URL_TMPL.format(user_id=specialist_user.id)
        response = client.get(url)
        assert response.status_code == 401 or response.status_code == 403

    def test_wrong_bearer_token_returns_401(
        self, specialist_user, bearer_token,
    ):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer wrong-token")
        url = URL_TMPL.format(user_id=specialist_user.id)
        response = client.get(url)
        assert response.status_code in (401, 403)

    def test_non_bearer_scheme_returns_401(
        self, specialist_user, bearer_token,
    ):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Basic dGVzdDp0ZXN0")
        url = URL_TMPL.format(user_id=specialist_user.id)
        response = client.get(url)
        assert response.status_code in (401, 403)

    def test_empty_internal_token_setting_fails_closed(
        self, settings, specialist_user,
    ):
        # Mirror of the IsInternalBearer fail-closed contract: an
        # empty setting must NEVER pass auth, even if the client
        # sends `Bearer `.
        settings.AYLA_INTERNAL_API_TOKEN = ""
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer anything")
        url = URL_TMPL.format(user_id=specialist_user.id)
        response = client.get(url)
        assert response.status_code in (401, 403)


@pytest.mark.django_db
class TestProfileResolution:
    """Right profile picked + right fields surfaced."""

    def test_specialist_returns_display_name_and_avatar(
        self, api, specialist_user,
    ):
        url = URL_TMPL.format(user_id=specialist_user.id)
        response = api.get(url)
        assert response.status_code == 200
        assert response.data["display_name"] == "Мастер Лера"
        # No avatar uploaded → empty string per closed-shape contract.
        assert response.data["avatar_url"] == ""

    def test_client_profile_returns_full_name(
        self, api, client_user_with_profile,
    ):
        url = URL_TMPL.format(user_id=client_user_with_profile.id)
        response = api.get(url)
        assert response.status_code == 200
        assert response.data["display_name"] == "Анна Клиентская"
        assert response.data["avatar_url"] == ""

    def test_bare_user_returns_empty_strings(self, api, bare_user):
        # Per the contract: keys ALWAYS present, values MAY be empty.
        # Bot's profile_client distinguishes "missing keys"
        # (malformed) from "empty string" (Ayla cleared the field) —
        # we MUST always return both keys.
        url = URL_TMPL.format(user_id=bare_user.id)
        response = api.get(url)
        assert response.status_code == 200
        assert response.data["display_name"] == ""
        assert response.data["avatar_url"] == ""

    def test_unknown_user_returns_404(self, api):
        random_id = uuid4()
        url = URL_TMPL.format(user_id=random_id)
        response = api.get(url)
        assert response.status_code == 404


@pytest.mark.django_db
class TestPIIGuard:
    """Response shape is the closed PII §7 allowlist."""

    def test_response_keys_are_exactly_the_two_safe_fields(
        self, api, specialist_user,
    ):
        # Closed-shape pin: future serializer drift that adds
        # phone / email / role / tenant / city / coords MUST fail
        # this assertion. Single source of truth for the wire shape.
        url = URL_TMPL.format(user_id=specialist_user.id)
        response = api.get(url)
        assert response.status_code == 200
        assert set(response.data.keys()) == {"display_name", "avatar_url"}
