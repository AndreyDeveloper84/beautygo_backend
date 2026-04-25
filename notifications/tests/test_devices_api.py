"""Tests for /api/v1/devices/register/ + /api/v1/devices/{id}/."""
import pytest
from rest_framework.test import APIClient

from users.models import DeviceToken, User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="dev1", password="x", role="client", phone="+79995550000",
    )


@pytest.fixture
def auth_client(user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=user)
    return c


REGISTER_URL = "/api/v1/devices/register/"


@pytest.mark.django_db
class TestDeviceRegister:
    def test_register_creates_active_token(self, auth_client, user):
        resp = auth_client.post(
            REGISTER_URL,
            {"token": "fcm-tok-1", "app_type": "client", "platform": "ios"},
            format="json",
        )
        assert resp.status_code == 201
        assert DeviceToken.objects.filter(
            user=user, token="fcm-tok-1", is_active=True,
        ).exists()

    def test_register_idempotent_returns_200_on_re_register(self, auth_client):
        auth_client.post(
            REGISTER_URL,
            {"token": "tok", "app_type": "client", "platform": "ios"},
            format="json",
        )
        resp = auth_client.post(
            REGISTER_URL,
            {"token": "tok", "app_type": "client", "platform": "ios"},
            format="json",
        )
        assert resp.status_code == 200
        assert DeviceToken.objects.filter(token="tok").count() == 1

    def test_register_rebinds_token_to_new_user(self, db, auth_client):
        # User A registers a token …
        u_b = User.objects.create_user(
            username="b", password="x", role="client", phone="+79996660000",
        )
        DeviceToken.objects.create(
            user=u_b, token="shared-tok", app_type="client",
            platform="ios", is_active=True,
        )
        # … User logs in on the same physical device — registers same token.
        resp = auth_client.post(
            REGISTER_URL,
            {"token": "shared-tok", "app_type": "client", "platform": "ios"},
            format="json",
        )
        assert resp.status_code == 200
        # Row migrated, no duplicate
        assert DeviceToken.objects.filter(token="shared-tok").count() == 1
        # Re-bound to the auth_client user
        token = DeviceToken.objects.get(token="shared-tok")
        assert token.user.username == "dev1"

    def test_register_validates_input(self, auth_client):
        resp = auth_client.post(
            REGISTER_URL,
            {"token": "tok", "app_type": "wrong", "platform": "ios"},
            format="json",
        )
        assert resp.status_code == 400

    def test_register_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(REGISTER_URL, {}, format="json")
        assert resp.status_code == 401


@pytest.mark.django_db
class TestDeviceDelete:
    def test_delete_own_soft_deactivates(self, auth_client, user):
        token = DeviceToken.objects.create(
            user=user, token="t", app_type="client",
            platform="ios", is_active=True,
        )
        resp = auth_client.delete(f"/api/v1/devices/{token.id}/")
        assert resp.status_code == 204
        token.refresh_from_db()
        # Soft delete preserves the row + sets is_active=False
        assert token.is_active is False
        assert DeviceToken.objects.filter(pk=token.id).exists()

    def test_delete_other_users_returns_404(self, db, auth_client):
        other = User.objects.create_user(
            username="u2", password="x", role="client", phone="+79997770000",
        )
        token = DeviceToken.objects.create(
            user=other, token="theirs", app_type="client",
            platform="ios", is_active=True,
        )
        resp = auth_client.delete(f"/api/v1/devices/{token.id}/")
        assert resp.status_code == 404
