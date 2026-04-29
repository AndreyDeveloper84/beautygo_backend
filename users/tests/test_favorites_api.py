"""Tests for the favorites API (DRF-72).

Per Notion API Spec v2.0 §FAVORITES:
  POST   /api/v1/favorites/specialists/{id}/   → 201 {data: {added: true}}
  DELETE /api/v1/favorites/specialists/{id}/   → 204
  GET    /api/v1/favorites/specialists/        → 200 {data: [SpecialistListItem]}

Coverage: auth + app-type, idempotent POST, idempotent DELETE,
unknown specialist 404 on POST (but 204 on DELETE), ownership scope.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from users.models import FavoriteSpecialist, SpecialistProfile, User


pytestmark = pytest.mark.django_db


LIST_URL = "/api/v1/favorites/specialists/"


def _detail_url(specialist_id) -> str:
    return f"/api/v1/favorites/specialists/{specialist_id}/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="fav-client", password="x", role="client",
        phone="+79994440000",
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username="fav-other", password="x", role="client",
        phone="+79994440001",
    )


@pytest.fixture
def specialist(db):
    user = User.objects.create_user(
        username="fav-spec", password="x", role="specialist",
        phone="+79995550000",
    )
    p = user.specialist_profile
    p.display_name = "Мастер"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.save()
    return p


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


# ---------------------------------------------------------------------------
# Auth + app-type
# ---------------------------------------------------------------------------


class TestAuthAndAppType:
    def test_list_unauthenticated_returns_401(self, specialist):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.get(LIST_URL)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_post_unauthenticated_returns_401(self, specialist):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(_detail_url(specialist.id))
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_pro_app_type_returns_403(self, client_user, specialist):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.post(_detail_url(specialist.id))
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# POST — add favorite
# ---------------------------------------------------------------------------


class TestAdd:
    def test_first_add_returns_201_added_true(
        self, auth_client, client_user, specialist,
    ):
        resp = auth_client.post(_detail_url(specialist.id))
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        assert resp.json()["data"] == {"added": True}
        assert FavoriteSpecialist.objects.filter(
            user=client_user, specialist=specialist,
        ).count() == 1

    def test_repeat_add_is_idempotent_returns_200(
        self, auth_client, client_user, specialist,
    ):
        # First add: 201.
        first = auth_client.post(_detail_url(specialist.id))
        assert first.status_code == status.HTTP_201_CREATED
        # Second add: 200, still added=true.
        second = auth_client.post(_detail_url(specialist.id))
        assert second.status_code == status.HTTP_200_OK
        assert second.json()["data"] == {"added": True}
        # Still one row, not two.
        assert FavoriteSpecialist.objects.filter(
            user=client_user, specialist=specialist,
        ).count() == 1

    def test_unknown_specialist_returns_404(self, auth_client):
        resp = auth_client.post(_detail_url(uuid.uuid4()))
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["error"]["code"] == "SPECIALIST_NOT_FOUND"


# ---------------------------------------------------------------------------
# DELETE — remove favorite
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_existing_returns_204(
        self, auth_client, client_user, specialist,
    ):
        FavoriteSpecialist.objects.create(
            user=client_user, specialist=specialist,
        )
        resp = auth_client.delete(_detail_url(specialist.id))
        assert resp.status_code == status.HTTP_204_NO_CONTENT
        assert FavoriteSpecialist.objects.filter(
            user=client_user, specialist=specialist,
        ).count() == 0

    def test_remove_non_favorited_is_idempotent_204(
        self, auth_client, specialist,
    ):
        # No row exists yet — DELETE should still return 204 so the
        # mobile retry-on-network-error path is safe.
        resp = auth_client.delete(_detail_url(specialist.id))
        assert resp.status_code == status.HTTP_204_NO_CONTENT

    def test_remove_unknown_specialist_id_is_204(self, auth_client):
        # Even if the specialist doesn't exist — same idempotent
        # semantics. The post-condition ("not favourited") is true.
        resp = auth_client.delete(_detail_url(uuid.uuid4()))
        assert resp.status_code == status.HTTP_204_NO_CONTENT

    def test_remove_does_not_touch_other_users(
        self, auth_client, client_user, other_user, specialist,
    ):
        FavoriteSpecialist.objects.create(
            user=other_user, specialist=specialist,
        )
        resp = auth_client.delete(_detail_url(specialist.id))
        assert resp.status_code == status.HTTP_204_NO_CONTENT
        # Other user's favourite is intact.
        assert FavoriteSpecialist.objects.filter(
            user=other_user, specialist=specialist,
        ).count() == 1


# ---------------------------------------------------------------------------
# GET — list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_returns_empty_data_array(self, auth_client):
        resp = auth_client.get(LIST_URL)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"] == []

    def test_returns_only_users_favorites(
        self, auth_client, client_user, other_user, specialist,
    ):
        FavoriteSpecialist.objects.create(
            user=client_user, specialist=specialist,
        )
        # Other user's favourite for the same specialist must not leak
        # into our list.
        FavoriteSpecialist.objects.create(
            user=other_user, specialist=specialist,
        )
        body = auth_client.get(LIST_URL).json()["data"]
        assert len(body) == 1
        assert body[0]["display_name"] == "Мастер"

    def test_orders_by_most_recent_first(self, auth_client, client_user, db):
        # Two specialists, second favourited later — should be first
        # in the list.
        a = User.objects.create_user(
            username="spec-a", password="x", role="specialist",
            phone="+79991111111",
        )
        b = User.objects.create_user(
            username="spec-b", password="x", role="specialist",
            phone="+79992222222",
        )
        for u, name in ((a, "Anna"), (b, "Boris")):
            p = u.specialist_profile
            p.display_name = name
            p.status = SpecialistProfile.ProfileStatus.ACTIVE
            p.save()

        # Stagger creates so SQLite microsecond-precision timestamps
        # differ; without this the order_by("-created_at") tie-break is
        # implementation-defined.
        from datetime import timedelta
        from django.utils import timezone

        first = FavoriteSpecialist.objects.create(
            user=client_user, specialist=a.specialist_profile,
        )
        FavoriteSpecialist.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(seconds=10),
        )
        FavoriteSpecialist.objects.create(
            user=client_user, specialist=b.specialist_profile,
        )
        body = auth_client.get(LIST_URL).json()["data"]
        names = [item["display_name"] for item in body]
        assert names == ["Boris", "Anna"]


# ---------------------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------------------


class TestCascade:
    def test_specialist_deleted_removes_favorites(
        self, auth_client, client_user, specialist,
    ):
        FavoriteSpecialist.objects.create(
            user=client_user, specialist=specialist,
        )
        specialist.user.delete()  # cascades through SpecialistProfile
        assert FavoriteSpecialist.objects.filter(
            user=client_user,
        ).count() == 0
