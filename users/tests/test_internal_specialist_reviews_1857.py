"""«Мои отзывы» мастера — под субъектом, без персданных клиента (DRF-1857, K14).

``GET /api/v1/internal/specialists/{id}/reviews/``

Что заперто:

- свой профиль — видимые отзывы, новые первыми; число и оценка по ним же,
  оценка только при отзыве (ноль — ``null``, не 0.0);
- клиент — «Имя Ф.», без имени — «Клиент», анонимный — ``null``; в теле нет
  фамилии целиком, телефона и ``username`` клиента;
- чужой профиль → 403, без ``X-External-User-ID`` → 403;
- чтение журналируется (§96): ``review_read``, объект — профиль мастера.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import Appointment
from privacy_audit.models import PersonalDataAccessLog
from reviews.models import Review
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1857"  # noqa: S105
ANNA = "bot:max:1857001"
OTHER = "bot:max:1857002"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="reviews1857", name="Reviews Salon")


def _master(salon, *, username, phone, external_id):
    user = User.objects.create_user(username=username, password="x", role="specialist", phone=phone)
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.save()
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False, linked_user=user,
    )
    category = ServiceCategory.objects.create(name=f"Cat {username}")
    service = Service.objects.create(
        specialist=profile, category=category, name="Маникюр",
        price=Decimal("1500"), duration_minutes=60,
    )
    return profile, service


@pytest.fixture
def anna(salon):
    return _master(salon, username="rev1857_anna", phone="+79991857101", external_id=ANNA)


@pytest.fixture
def other(salon):
    return _master(salon, username="rev1857_other", phone="+79991857102", external_id=OTHER)


def _review(master, *, username, phone, rating=5, first_name="", last_name="",
            anonymous=False, hidden=False, text="Спасибо", hours_ago=2) -> Review:
    profile, service = master
    client = User.objects.create_user(
        username=username, password="x", role="client", phone=phone,
        first_name=first_name, last_name=last_name,
    )
    start = timezone.now() - timezone.timedelta(hours=hours_ago)
    appointment = Appointment.objects.create(
        client=client, specialist=profile, service=service,
        start_datetime=start, end_datetime=start + timezone.timedelta(hours=1),
        status="completed", price=service.price,
    )
    return Review.objects.create(
        appointment=appointment, client=client, specialist=profile, service=service,
        rating=rating, text=text, is_anonymous=anonymous, is_hidden=hidden,
    )


def _client(*, actor: str | None = ANNA) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _url(profile) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/reviews/"


def _get(profile, **kwargs):
    resp = _client(**kwargs).get(_url(profile))
    body = resp.json()
    return resp, body.get("data", body)


class TestOwnReviews:
    def test_visible_reviews_newest_first_with_count_and_rating(self, anna):
        _review(anna, username="rev1857_c1", phone="+79991857201", rating=5, hours_ago=30)
        _review(anna, username="rev1857_c2", phone="+79991857202", rating=4, hours_ago=3)
        _review(anna, username="rev1857_c3", phone="+79991857203", rating=1, hidden=True)

        resp, data = _get(anna[0])

        assert resp.status_code == 200, resp.content
        assert data["review_count"] == 2
        assert data["rating"] == 4.5
        assert [r["rating"] for r in data["reviews"]] == [4, 5]

    def test_no_reviews_is_no_rating_not_zero(self, anna):
        resp, data = _get(anna[0])
        assert resp.status_code == 200, resp.content
        assert data == {"specialist_id": str(anna[0].pk), "review_count": 0, "rating": None, "reviews": []}

    def test_only_hidden_reviews_count_as_none(self, anna):
        _review(anna, username="rev1857_c4", phone="+79991857204", hidden=True)
        _, data = _get(anna[0])
        assert data["review_count"] == 0 and data["rating"] is None and data["reviews"] == []


class TestClientName:
    def test_named_client_is_first_name_and_initial(self, anna):
        _review(anna, username="user_79991857301", phone="+79991857301",
                first_name="Ксения", last_name="Леонова")
        resp, data = _get(anna[0])
        assert data["reviews"][0]["client_name"] == "Ксения Л."
        body = resp.content.decode("utf-8")
        assert "Леонова" not in body and "79991857301" not in body

    @pytest.mark.parametrize("username", ["bot:max:18570077", "user_79991857302"])
    def test_nameless_client_is_neutral(self, anna, username):
        _review(anna, username=username, phone="+79991857399")
        resp, data = _get(anna[0])
        assert data["reviews"][0]["client_name"] == "Клиент"
        body = resp.content.decode("utf-8")
        assert username not in body and "79991857399" not in body

    def test_anonymous_review_has_no_name(self, anna):
        _review(anna, username="rev1857_anon", phone="+79991857303", first_name="Ксения", anonymous=True)
        resp, data = _get(anna[0])
        assert data["reviews"][0]["client_name"] is None
        assert "Ксения" not in resp.content.decode("utf-8")


class TestSubject:
    def test_foreign_profile_is_403(self, anna, other):
        _review(other, username="rev1857_c5", phone="+79991857205", text="Секрет соседа")
        resp = _client().get(_url(other[0]))
        assert resp.status_code == 403
        assert "Секрет соседа" not in resp.content.decode("utf-8")

    def test_unnamed_caller_is_403(self, anna):
        assert _client(actor=None).get(_url(anna[0])).status_code == 403


class TestJournal:
    def test_the_read_is_journalled(self, anna):
        _review(anna, username="rev1857_c6", phone="+79991857206")
        before = PersonalDataAccessLog.objects.count()
        resp, _ = _get(anna[0])
        assert resp.status_code == 200
        row = PersonalDataAccessLog.objects.order_by("-id").first()
        assert PersonalDataAccessLog.objects.count() == before + 1
        assert row.operation == PersonalDataAccessLog.Operation.REVIEW_READ
        assert row.object_category == PersonalDataAccessLog.ObjectCategory.REVIEW
        assert str(row.object_id) == str(anna[0].pk)
