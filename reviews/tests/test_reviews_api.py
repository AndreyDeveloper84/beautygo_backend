"""Tests for DRF-96: Reviews & Ratings API."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from appointments.models import Appointment
from reviews.models import Review
from services.models import Service, ServiceCategory
from users.models import User

REVIEWS_URL = '/api/v1/reviews/'
SPECIALISTS_URL = '/api/v1/specialists/'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Ногти')


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username='rev_specialist', password='pass',
        role='specialist', phone='+79990600010',
    )
    profile = user.specialist_profile
    profile.display_name = 'Мастер Иван'
    profile.status = 'active'
    profile.is_available = True
    profile.save()
    return user


@pytest.fixture
def service(db, specialist_user, category):
    return Service.objects.create(
        specialist=specialist_user.specialist_profile,
        category=category,
        name='Маникюр',
        price=Decimal('1500'),
        duration_minutes=60,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='rev_client', password='pass',
        role='client', phone='+79990600001',
    )


@pytest.fixture
def client_app(db, client_user):
    api = APIClient()
    api.defaults['HTTP_X_APP_TYPE'] = 'client'
    api.force_authenticate(user=client_user)
    return api


@pytest.fixture
def anon_app(db):
    api = APIClient()
    api.defaults['HTTP_X_APP_TYPE'] = 'client'
    return api


def _make_appointment(client, specialist_user, service, status_val='completed'):
    now = timezone.now()
    profile = specialist_user.specialist_profile
    return Appointment.objects.create(
        client=client,
        specialist=profile,
        service=service,
        start_datetime=now - timezone.timedelta(hours=2),
        end_datetime=now - timezone.timedelta(hours=1),
        status=status_val,
        price=service.price,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/reviews/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestReviewCreate:

    def test_create_review_success(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        response = client_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 5,
            'text': 'Отличная работа!',
        }, format='json')

        assert response.status_code == status.HTTP_201_CREATED
        data = response.data['data']
        assert data['rating'] == 5
        assert data['text'] == 'Отличная работа!'
        assert data['is_anonymous'] is False
        assert Review.objects.count() == 1

    def test_create_review_anonymous(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        response = client_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 4,
            'is_anonymous': True,
        }, format='json')

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['data']['is_anonymous'] is True
        assert response.data['data']['client_name'] is None

    def test_create_review_updates_specialist_rating(
        self, client_app, client_user, specialist_user, service,
    ):
        appt = _make_appointment(client_user, specialist_user, service)
        client_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 4,
        }, format='json')

        specialist_user.specialist_profile.refresh_from_db()
        assert specialist_user.specialist_profile.rating == Decimal('4.0')
        assert specialist_user.specialist_profile.reviews_count == 1

    def test_create_review_appointment_not_completed(
        self, client_app, client_user, specialist_user, service,
    ):
        appt = _make_appointment(client_user, specialist_user, service, status_val='confirmed')
        response = client_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 5,
        }, format='json')

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'APPOINTMENT_NOT_COMPLETED'

    def test_create_review_duplicate(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        client_app.post(REVIEWS_URL, {'appointment_id': str(appt.id), 'rating': 5}, format='json')
        response = client_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 3,
        }, format='json')

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'REVIEW_ALREADY_EXISTS'

    def test_create_review_not_own_appointment(
        self, client_app, specialist_user, service, db,
    ):
        other_client = User.objects.create_user(
            username='other_client', password='pass',
            role='client', phone='+79990600099',
        )
        appt = _make_appointment(other_client, specialist_user, service)
        response = client_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 5,
        }, format='json')

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_review_nonexistent_appointment(self, client_app):
        response = client_app.post(REVIEWS_URL, {
            'appointment_id': str(uuid.uuid4()),
            'rating': 5,
        }, format='json')
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_review_unauthenticated(self, anon_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        response = anon_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 5,
        }, format='json')
        assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

    def test_create_review_invalid_rating(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        response = client_app.post(REVIEWS_URL, {
            'appointment_id': str(appt.id),
            'rating': 10,
        }, format='json')
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# GET /api/v1/specialists/{id}/reviews/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSpecialistReviewsList:

    def _create_review(self, client_user, specialist_user, service, rating=5, text='', hidden=False):
        appt = _make_appointment(client_user, specialist_user, service)
        return Review.objects.create(
            appointment=appt,
            client=client_user,
            specialist=specialist_user.specialist_profile,
            service=service,
            rating=rating,
            text=text,
            is_hidden=hidden,
        )

    def test_list_reviews_empty(self, anon_app, specialist_user):
        url = f"{SPECIALISTS_URL}{specialist_user.specialist_profile.pk}/reviews/"
        response = anon_app.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data'] == []
        assert response.data['meta']['count'] == 0

    def test_list_reviews_with_data(self, anon_app, client_user, specialist_user, service):
        self._create_review(client_user, specialist_user, service, rating=5, text='Хорошо')
        url = f"{SPECIALISTS_URL}{specialist_user.specialist_profile.pk}/reviews/"
        response = anon_app.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['data']) == 1
        assert response.data['meta']['count'] == 1
        assert response.data['data'][0]['rating'] == 5

    def test_hidden_reviews_excluded(self, anon_app, client_user, specialist_user, service, db):
        other = User.objects.create_user(
            username='client2', password='pass', role='client', phone='+79990600088',
        )
        self._create_review(client_user, specialist_user, service, rating=5)
        self._create_review(other, specialist_user, service, rating=1, hidden=True)

        url = f"{SPECIALISTS_URL}{specialist_user.specialist_profile.pk}/reviews/"
        response = anon_app.get(url)

        assert response.data['meta']['count'] == 1

    def test_sort_by_rating(self, anon_app, client_user, specialist_user, service, db):
        other = User.objects.create_user(
            username='client3', password='pass', role='client', phone='+79990600077',
        )
        self._create_review(client_user, specialist_user, service, rating=3)
        self._create_review(other, specialist_user, service, rating=5)

        url = f"{SPECIALISTS_URL}{specialist_user.specialist_profile.pk}/reviews/?sort=rating"
        response = anon_app.get(url)

        ratings = [r['rating'] for r in response.data['data']]
        assert ratings == sorted(ratings, reverse=True)

    def test_anonymous_review_hides_name(self, anon_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        Review.objects.create(
            appointment=appt,
            client=client_user,
            specialist=specialist_user.specialist_profile,
            service=service,
            rating=4,
            is_anonymous=True,
        )
        url = f"{SPECIALISTS_URL}{specialist_user.specialist_profile.pk}/reviews/"
        response = anon_app.get(url)

        assert response.data['data'][0]['client_name'] is None

    def test_specialist_not_found(self, anon_app):
        url = f"{SPECIALISTS_URL}{uuid.uuid4()}/reviews/"
        response = anon_app.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND
