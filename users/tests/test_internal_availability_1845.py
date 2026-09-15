"""«Принимаю записи» из кабинета мастера — под субъектом (DRF-1845, K1a).

``GET/PATCH /api/v1/internal/specialists/{id}/availability/`` пишет
``is_booking_enabled`` (решение владельца, DRF-1349, 15.09: пауза приёма без
деактивации профиля; ``is_available`` не трогается).

Что заперто:

- свой опубликованный профиль: пауза и снятие паузы, GET читает то же;
- строго булево: строка ``"false"`` — 400, не пауза;
- чужой профиль → 403, флаг не тронут; без ``X-External-User-ID`` → 403;
- владелец DRAFT-workspace (pre-LINKED claim, M28) — не принимает клиентов:
  claim пускает к настройке, приём — не настройка; флаг не тронут.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from tenants.models import Tenant
from tenants.solo_provisioning import provision_solo_workspace
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1845"  # noqa: S105
ANNA = "bot:max:1845001"
OTHER = "bot:max:1845002"
DRAFT_OWNER = "bot:max:1845003"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="avail1845", name="Availability Salon")


def _master(salon, *, username, phone, external_id):
    user = User.objects.create_user(username=username, password="x", role="specialist", phone=phone)
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False, linked_user=user,
    )
    return profile


@pytest.fixture
def anna(salon):
    return _master(salon, username="avail1845_anna", phone="+79991845201", external_id=ANNA)


@pytest.fixture
def other(salon):
    return _master(salon, username="avail1845_other", phone="+79991845202", external_id=OTHER)


def _client(*, actor: str | None = ANNA) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _url(profile_id) -> str:
    return f"/api/v1/internal/specialists/{profile_id}/availability/"


def _flag(profile) -> bool:
    return SpecialistProfile.objects.values_list("is_booking_enabled", flat=True).get(pk=profile.pk)


def _data(resp):
    body = resp.json()
    return body.get("data", body)


class TestOwnProfile:
    def test_pause_and_resume(self, anna):
        resp = _client().patch(_url(anna.pk), {"accepting_bookings": False}, format="json")
        assert resp.status_code == 200, resp.content
        assert _data(resp)["accepting_bookings"] is False
        assert _flag(anna) is False

        resp = _client().patch(_url(anna.pk), {"accepting_bookings": True}, format="json")
        assert resp.status_code == 200, resp.content
        assert _flag(anna) is True

    def test_get_reads_the_flag(self, anna):
        anna.is_booking_enabled = False
        anna.save(update_fields=["is_booking_enabled"])
        resp = _client().get(_url(anna.pk))
        assert resp.status_code == 200, resp.content
        assert _data(resp)["accepting_bookings"] is False

    def test_is_available_is_not_touched(self, anna):
        _client().patch(_url(anna.pk), {"accepting_bookings": False}, format="json")
        anna.refresh_from_db()
        assert anna.is_available is True

    @pytest.mark.parametrize("body", [{"accepting_bookings": "false"}, {"accepting_bookings": 0}, {}])
    def test_only_a_boolean_is_accepted(self, anna, body):
        resp = _client().patch(_url(anna.pk), body, format="json")
        assert resp.status_code == 400, resp.content
        assert _flag(anna) is True


class TestSubject:
    def test_foreign_profile_is_403_and_untouched(self, anna, other):
        resp = _client().patch(_url(other.pk), {"accepting_bookings": False}, format="json")
        assert resp.status_code == 403, resp.content
        assert _flag(other) is True

    def test_unnamed_caller_is_403(self, anna):
        resp = _client(actor=None).patch(_url(anna.pk), {"accepting_bookings": False}, format="json")
        assert resp.status_code == 403
        assert _flag(anna) is True


class TestDraftWorkspaceTakesNoClients:
    def test_pre_linked_owner_cannot_open_bookings(self, db):
        """ayla-96, 15.09: pre-LINKED — setup authority, не приём клиентов.
        Workspace заводится живой провизией, как в DRF-1829, — тест верен и
        после сужения claim в DRF-1874."""
        workspace = provision_solo_workspace(
            tenant_id=uuid.uuid4(),
            slug="solo-max-1845draft",
            name="Студия 1845",
            city="Пенза",
            external_user_id=DRAFT_OWNER,
            display_name="Мастер",
        )
        profile = workspace.profile
        assert profile.status == SpecialistProfile.ProfileStatus.DRAFT
        before = _flag(profile)
        User.objects.get_or_create(
            username=DRAFT_OWNER, defaults={"role": "client", "is_proxy": True, "is_guest": False},
        )

        resp = _client(actor=DRAFT_OWNER).patch(
            _url(profile.pk), {"accepting_bookings": not before}, format="json",
        )

        assert resp.status_code != 200, resp.content
        if resp.status_code == 409:
            assert resp.json()["error"]["code"] == "PROFILE_NOT_ACTIVE"
        assert _flag(profile) is before
