"""DRF-230 — UserPersonalContext API tests.

Covers the 5 endpoints under ``/api/v1/users/me/personal-context/``:

- GET creates the row lazily and returns the green-zone fields.
- PATCH updates a subset and persists, validating bounded fields.
- POST /skip/ increments the per-field skipped_questions counter.
- DELETE /<field>/ resets a single field to its default (152-ФЗ).
- DELETE / wipes the row entirely (152-ФЗ "очистить историю").

What's NOT covered here (separate PRs of DRF-230):
- Celery infer_user_patterns task that updates favorite_masters /
  busy_days from Appointment history.
- PersonalizationEngine anti-spam rules (skip × 2 → 30-day pause).
- Yellow / red zone fields with extra encryption.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from users.models import UserPersonalContext


User = get_user_model()

PC_URL = "/api/v1/auth/users/me/personal-context/"
PC_SKIP_URL = "/api/v1/auth/users/me/personal-context/skip/"


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="pc_owner", password="x",
        role="client", phone="+79991230001",
    )


@pytest.fixture
def auth_client(client_user):
    # X-App-Type: client header required by AppTypeMiddleware on
    # all /api/v1/ paths (except service-to-service prefixes).
    api = APIClient(HTTP_X_APP_TYPE="client")
    api.force_authenticate(user=client_user)
    return api


@pytest.mark.django_db
class TestGetPersonalContext:
    def test_lazy_creates_on_first_get(self, auth_client, client_user):
        assert not UserPersonalContext.objects.filter(user=client_user).exists()
        resp = auth_client.get(PC_URL)
        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        # All green-zone fields present, defaults applied.
        assert body["preferred_districts"] == []
        assert body["workplace_district"] == ""
        assert body["home_district"] == ""
        assert body["favorite_masters"] == []
        assert body["min_rating_preference"] is None
        assert body["busy_days"] == []
        # Service fields readable but empty.
        assert body["skipped_questions"] == {}
        assert UserPersonalContext.objects.filter(user=client_user).count() == 1

    def test_subsequent_get_returns_same_row(self, auth_client, client_user):
        first = auth_client.get(PC_URL).json().get("data", {})
        second = auth_client.get(PC_URL).json().get("data", {})
        assert first["id"] == second["id"]
        assert UserPersonalContext.objects.filter(user=client_user).count() == 1

    def test_unauthenticated_blocked(self):
        api = APIClient()
        resp = api.get(PC_URL)
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestPatchPersonalContext:
    def test_patch_updates_simple_fields(self, auth_client, client_user):
        auth_client.get(PC_URL)  # lazy-create
        resp = auth_client.patch(PC_URL, {
            "workplace_district": "Арбатская",
            "home_district": "Сокольники",
            "min_rating_preference": 4.5,
            "busy_days": ["sat", "sun"],
        }, format="json")
        assert resp.status_code == 200, resp.content
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.workplace_district == "Арбатская"
        assert ctx.home_district == "Сокольники"
        assert ctx.min_rating_preference == 4.5
        assert ctx.busy_days == ["sat", "sun"]

    def test_patch_partial_does_not_clear_unspecified(
        self, auth_client, client_user,
    ):
        auth_client.patch(PC_URL, {
            "workplace_district": "Арбатская",
            "home_district": "Сокольники",
        }, format="json")
        # Patch with only one field — other should survive.
        auth_client.patch(PC_URL, {
            "workplace_district": "Тверская",
        }, format="json")
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.workplace_district == "Тверская"
        assert ctx.home_district == "Сокольники"

    def test_patch_validates_min_rating_range(self, auth_client):
        resp = auth_client.patch(
            PC_URL, {"min_rating_preference": 9.9}, format="json",
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "min_rating_preference" in str(body)

    def test_patch_validates_time_slots(self, auth_client):
        resp = auth_client.patch(PC_URL, {
            "preferred_time_slots": ["morning", "bogus_slot"],
        }, format="json")
        assert resp.status_code == 400

    def test_service_fields_are_read_only(self, auth_client, client_user):
        # Even if the user sends skipped_questions, server ignores them.
        auth_client.patch(PC_URL, {
            "skipped_questions": {"workplace_district": {"count": 99}},
        }, format="json")
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.skipped_questions == {}


@pytest.mark.django_db
class TestSkipQuestion:
    def test_skip_increments_counter(self, auth_client, client_user):
        resp = auth_client.post(
            PC_SKIP_URL, {"field": "workplace_district"}, format="json",
        )
        assert resp.status_code == 200
        ctx = UserPersonalContext.objects.get(user=client_user)
        entry = ctx.skipped_questions["workplace_district"]
        assert entry["count"] == 1
        assert "last_at" in entry

    def test_skip_twice_records_count_2(self, auth_client, client_user):
        auth_client.post(
            PC_SKIP_URL, {"field": "workplace_district"}, format="json",
        )
        auth_client.post(
            PC_SKIP_URL, {"field": "workplace_district"}, format="json",
        )
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.skipped_questions["workplace_district"]["count"] == 2

    def test_skip_unknown_field_400(self, auth_client):
        resp = auth_client.post(
            PC_SKIP_URL, {"field": "bogus"}, format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestDeleteField:
    def test_resets_string_field_to_empty(self, auth_client, client_user):
        auth_client.patch(
            PC_URL, {"workplace_district": "Арбатская"}, format="json",
        )
        resp = auth_client.delete(f"{PC_URL}workplace_district/")
        assert resp.status_code == 204
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.workplace_district == ""

    def test_resets_list_field_to_empty(self, auth_client, client_user):
        auth_client.patch(
            PC_URL, {"busy_days": ["sat", "sun"]}, format="json",
        )
        resp = auth_client.delete(f"{PC_URL}busy_days/")
        assert resp.status_code == 204
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.busy_days == []

    def test_resets_nullable_field_to_none(self, auth_client, client_user):
        auth_client.patch(
            PC_URL, {"min_rating_preference": 4.5}, format="json",
        )
        resp = auth_client.delete(f"{PC_URL}min_rating_preference/")
        assert resp.status_code == 204
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.min_rating_preference is None

    def test_service_field_not_resettable_404(self, auth_client):
        # skipped_questions is service-side; user can't wipe it.
        resp = auth_client.delete(f"{PC_URL}skipped_questions/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestTotalWipe:
    def test_delete_removes_row_152fz(self, auth_client, client_user):
        auth_client.patch(
            PC_URL, {"workplace_district": "Арбатская"}, format="json",
        )
        assert UserPersonalContext.objects.filter(user=client_user).exists()
        resp = auth_client.delete(PC_URL)
        assert resp.status_code == 204
        assert not UserPersonalContext.objects.filter(user=client_user).exists()

    def test_get_after_wipe_lazy_creates_fresh(self, auth_client, client_user):
        auth_client.patch(
            PC_URL, {"workplace_district": "Арбатская"}, format="json",
        )
        auth_client.delete(PC_URL)  # wipe
        resp = auth_client.get(PC_URL)
        body = resp.json().get("data", resp.json())
        assert body["workplace_district"] == ""
