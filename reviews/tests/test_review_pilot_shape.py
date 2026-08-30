"""DRF-1421 — отзыв за бронь ПИЛОТА: канон наполнен, легаси `Service` пуст.

Замер боевого пилота (2026-08-30)::

    SpecialistService   292
    SalonService         94
    Service (легаси)      0
    Review                0

`Review.service` был обязательной ссылкой (`null=True` не стоял) на
легаси-таблицу, в которой ноль строк. Отзыв за пилотную бронь нельзя
было создать физически: `Review.objects.create(service=appointment
.service)` получал `None` — бронь пилота несёт `salon_service`, а
`service IS NULL` по CHECK `appointment_exactly_one_service_source` —
и падал на NOT NULL. Это не «отзыв не приходит», это «отзыв не
сохранится, даже если человек дошёл до формы и нажал отправить».

Фикстуры повторяют форму пилота буквально: ни одной легаси-строки.
Каждый тест сначала утверждает `Service.objects.count() == 0`.

Правило контура: отрицательному утверждению нужна положительная стража
на тех же данных. «Не упало» проходит и на пустой выборке, поэтому
рядом с каждым «сохранилось» стоит ненулевое утверждение о содержимом:
имя услуги на проводе, пересчитанный рейтинг мастера.

Легаси не выключается, а дополняется (как в #267): маркетплейсный
тенант через Pro-приложение пишет ровно в `Service`, и отзыв за такую
бронь обязан сохраняться ровно как раньше. Отдельный класс это держит.

Никаких литеральных дат — только смещения от `now`.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from appointments.models import Appointment
from reviews.models import Review
from services.models import (
    SalonService,
    Service,
    ServiceCategory,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

REVIEWS_URL = "/api/v1/reviews/"
SPECIALISTS_URL = "/api/v1/specialists/"

SALON_SERVICE_NAME = "Окрашивание в один тон"
MARKETPLACE_SERVICE_NAME = "Классический маникюр"


# ---------------------------------------------------------------------------
# Фикстуры — форма пилота
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="drf1421-tenant", name="DRF1421 Salon")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="DRF1421 Волосы", slug="drf1421-hair")


@pytest.fixture
def specialist(db, tenant):
    user = User.objects.create_user(
        username="drf1421_spec", password="x", role="specialist",
        phone="+79990614210",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = "Мастер DRF1421"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.save()
    return profile


@pytest.fixture
def salon_service(tenant, category):
    """Канонический слой — то, что реально наполняет приёмка пилота."""
    return SalonService.objects.create(
        tenant=tenant, category=category, name=SALON_SERVICE_NAME,
        duration_minutes=90, base_price=Decimal("3500.00"), is_active=True,
    )


@pytest.fixture
def salon_link(salon_service, specialist):
    return SpecialistService.objects.create(
        salon_service=salon_service, specialist=specialist,
        duration_minutes=None, price=Decimal("3500.00"), is_active=True,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="drf1421_client", password="x", role="client",
        phone="+79990614211",
    )


@pytest.fixture
def client_app(client_user):
    api = APIClient()
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    api.force_authenticate(user=client_user)
    return api


@pytest.fixture
def anon_app(db):
    api = APIClient()
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    return api


def _completed_appointment(client_user, specialist, *, salon=None, legacy=None):
    """Завершённая бронь. Ровно одна типизированная ссылка на услугу."""
    now = timezone.now()
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist,
        service=legacy,
        salon_service=salon,
        start_datetime=now - timezone.timedelta(hours=3),
        end_datetime=now - timezone.timedelta(hours=2),
        status=Appointment.Status.COMPLETED,
        price=Decimal("3500.00"),
        snapshot_service_name=(salon or legacy).name,
    )


# ---------------------------------------------------------------------------
# Форма пилота: отзыв обязан сохраняться
# ---------------------------------------------------------------------------


class TestReviewOnPilotShape:

    def test_review_is_creatable_for_a_salon_catalog_booking(
        self, client_app, client_user, specialist, salon_service, salon_link,
    ):
        """Главный дефект DRF-1421: на форме пилота отзыв не сохранялся."""
        # Стража: легаси пуст — иначе фикстура «починит» проверку молча.
        assert Service.objects.count() == 0

        appointment = _completed_appointment(
            client_user, specialist, salon=salon_service,
        )
        assert appointment.service_id is None
        assert appointment.salon_service_id == salon_service.id

        response = client_app.post(REVIEWS_URL, {
            "appointment_id": str(appointment.id),
            "rating": 5,
            "text": "Цвет ровно как договаривались.",
        }, format="json")

        assert response.status_code == status.HTTP_201_CREATED, response.data

        # Положительная стража: не «не упало», а сохранилось с содержимым.
        assert Review.objects.count() == 1
        review = Review.objects.get()
        assert review.salon_service_id == salon_service.id
        assert review.service_id is None
        assert review.rating == 5
        assert review.text == "Цвет ровно как договаривались."

        # Легаси так и не наполнился по дороге.
        assert Service.objects.count() == 0

    def test_salon_review_carries_the_service_name_on_the_wire(
        self, client_app, client_user, specialist, salon_service, salon_link,
    ):
        """`service_name` читает канон, а не отдаёт null."""
        assert Service.objects.count() == 0

        appointment = _completed_appointment(
            client_user, specialist, salon=salon_service,
        )
        response = client_app.post(REVIEWS_URL, {
            "appointment_id": str(appointment.id), "rating": 4,
        }, format="json")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["data"]["service_name"] == SALON_SERVICE_NAME
        assert Service.objects.count() == 0

    def test_salon_review_reaches_the_public_listing_with_its_service_name(
        self, client_app, anon_app, client_user, specialist,
        salon_service, salon_link,
    ):
        """Публичный листинг мастера показывает отзыв за салонную бронь."""
        assert Service.objects.count() == 0

        appointment = _completed_appointment(
            client_user, specialist, salon=salon_service,
        )
        create = client_app.post(REVIEWS_URL, {
            "appointment_id": str(appointment.id),
            "rating": 5, "text": "Вернусь.",
        }, format="json")
        assert create.status_code == status.HTTP_201_CREATED, create.data

        listing = anon_app.get(f"{SPECIALISTS_URL}{specialist.id}/reviews/")
        assert listing.status_code == status.HTTP_200_OK, listing.data
        rows = listing.data["data"]
        assert len(rows) == 1
        assert rows[0]["service_name"] == SALON_SERVICE_NAME
        assert rows[0]["rating"] == 5
        assert Service.objects.count() == 0

    def test_salon_review_recalculates_the_specialist_rating(
        self, client_app, client_user, specialist, salon_service, salon_link,
    ):
        """Рейтинг денормализован; на форме пилота он обязан поехать."""
        assert Service.objects.count() == 0
        assert specialist.reviews_count == 0

        appointment = _completed_appointment(
            client_user, specialist, salon=salon_service,
        )
        response = client_app.post(REVIEWS_URL, {
            "appointment_id": str(appointment.id), "rating": 4,
        }, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data

        specialist.refresh_from_db()
        assert specialist.reviews_count == 1
        assert float(specialist.rating) == pytest.approx(4.0)
        assert Service.objects.count() == 0


# ---------------------------------------------------------------------------
# Объединение, а не замена: маркетплейсная бронь работает как раньше
# ---------------------------------------------------------------------------


class TestMarketplaceReviewStillWorks:

    @pytest.fixture
    def marketplace_service(self, specialist, category):
        return Service.objects.create(
            specialist=specialist, category=category,
            name=MARKETPLACE_SERVICE_NAME,
            price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        )

    def test_review_for_a_legacy_marketplace_booking_is_unchanged(
        self, client_app, client_user, specialist, marketplace_service,
    ):
        """Легаси-слой жив по схеме — Pro-приложение пишет ровно в него."""
        assert Service.objects.count() == 1

        appointment = _completed_appointment(
            client_user, specialist, legacy=marketplace_service,
        )
        response = client_app.post(REVIEWS_URL, {
            "appointment_id": str(appointment.id),
            "rating": 5, "text": "Как всегда аккуратно.",
        }, format="json")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["data"]["service_name"] == MARKETPLACE_SERVICE_NAME

        review = Review.objects.get()
        assert review.service_id == marketplace_service.id
        assert review.salon_service_id is None


# ---------------------------------------------------------------------------
# CHECK кусается: ровно одна ссылка, не ноль и не две
# ---------------------------------------------------------------------------


class TestExactlyOneServiceSource:
    """Без этого класса «обнулили обе ссылки» прошло бы незамеченным.

    Обнуляемость нужна была только чтобы дать салонной броне куда
    сослаться. Отзыв без услуги вообще — не цель правки, и БД обязана
    его отвергать так же, как отвергает бронь без услуги.
    """

    def test_both_references_null_is_rejected(
        self, client_user, specialist, salon_service, salon_link,
    ):
        assert Service.objects.count() == 0
        appointment = _completed_appointment(
            client_user, specialist, salon=salon_service,
        )
        with pytest.raises(IntegrityError, match="review_exactly_one_service_source"):
            with transaction.atomic():
                Review.objects.create(
                    appointment=appointment, client=client_user,
                    specialist=specialist, service=None, salon_service=None,
                    rating=5,
                )
        assert Review.objects.count() == 0

    def test_both_references_set_is_rejected(
        self, client_user, specialist, category, salon_service, salon_link,
    ):
        legacy = Service.objects.create(
            specialist=specialist, category=category,
            name=MARKETPLACE_SERVICE_NAME,
            price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        )
        appointment = _completed_appointment(
            client_user, specialist, salon=salon_service,
        )
        with pytest.raises(IntegrityError, match="review_exactly_one_service_source"):
            with transaction.atomic():
                Review.objects.create(
                    appointment=appointment, client=client_user,
                    specialist=specialist, service=legacy,
                    salon_service=salon_service, rating=5,
                )
        assert Review.objects.count() == 0
