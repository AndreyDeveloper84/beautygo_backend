"""Tests for Specialists list API (DRF-60)."""

import logging
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from services.models import Service, ServiceCategory
from users.models import User

logger = logging.getLogger(__name__)

SPECIALISTS_URL = '/api/v1/specialists/'


@pytest.fixture
def client_app(db):
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'client'
    user = User.objects.create_user(
        username='spec_api_client', password='pass',
        role='client', phone='+79990500001',
    )
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def active_specialist(db):
    """Create an active specialist with services."""
    user = User.objects.create_user(
        username='spec_active', password='pass',
        role='specialist', phone='+79990500010',
    )
    profile = user.specialist_profile
    profile.display_name = 'Елена Мастер'
    profile.status = 'active'
    profile.is_available = True
    profile.rating = Decimal('4.8')
    profile.reviews_count = 25
    profile.address = 'Казань, ул. Баумана 1'
    profile.location_lat = Decimal('55.796127')
    profile.location_lng = Decimal('49.106405')
    profile.save()

    Service.objects.create(
        specialist=user, name='Маникюр',
        price=Decimal('1500'), duration_minutes=60,
    )
    Service.objects.create(
        specialist=user, name='Педикюр',
        price=Decimal('2000'), duration_minutes=90,
    )
    return user


@pytest.fixture
def draft_specialist(db):
    """Specialist with draft status — should NOT appear in search."""
    user = User.objects.create_user(
        username='spec_draft', password='pass',
        role='specialist', phone='+79990500020',
    )
    profile = user.specialist_profile
    profile.display_name = 'Черновик Мастер'
    profile.status = 'draft'
    profile.save()
    return user


@pytest.mark.django_db
class TestSpecialistList:

    def test_list_active_specialists(
        self, client_app, active_specialist, draft_specialist,
    ):
        response = client_app.get(SPECIALISTS_URL)
        assert response.status_code == status.HTTP_200_OK
        names = [s['display_name'] for s in response.data]
        assert 'Елена Мастер' in names
        assert 'Черновик Мастер' not in names

    def test_includes_services_preview(self, client_app, active_specialist):
        response = client_app.get(SPECIALISTS_URL)
        assert response.status_code == status.HTTP_200_OK
        specialist = response.data[0]
        assert 'services_preview' in specialist
        assert len(specialist['services_preview']) == 2
        assert specialist['services_count'] == 2

    def test_includes_rating_and_reviews(
        self, client_app, active_specialist,
    ):
        response = client_app.get(SPECIALISTS_URL)
        specialist = response.data[0]
        assert float(specialist['rating']) == 4.8
        assert specialist['reviews_count'] == 25

    def test_filter_by_category(self, client_app, active_specialist):
        cat = ServiceCategory.objects.create(name='Ногти фильтр спец')
        Service.objects.filter(
            specialist=active_specialist, name='Маникюр',
        ).update(category=cat)

        response = client_app.get(
            SPECIALISTS_URL, {'category_id': cat.pk},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1

    def test_filter_by_min_rating(self, client_app, active_specialist):
        response = client_app.get(
            SPECIALISTS_URL, {'min_rating': 4.5},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1

        response = client_app.get(
            SPECIALISTS_URL, {'min_rating': 5.0},
        )
        assert len(response.data) == 0

    def test_filter_by_price_range(self, client_app, active_specialist):
        response = client_app.get(
            SPECIALISTS_URL, {'max_price': 1000},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 0

        response = client_app.get(
            SPECIALISTS_URL, {'max_price': 2000},
        )
        assert len(response.data) == 1

    def test_geo_filter(self, client_app, active_specialist):
        # Kazan center — should find specialist at Bauman 1
        response = client_app.get(
            SPECIALISTS_URL,
            {'lat': '55.796', 'lon': '49.106', 'radius': '5'},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1

        # Moscow — too far
        response = client_app.get(
            SPECIALISTS_URL,
            {'lat': '55.755', 'lon': '37.617', 'radius': '5'},
        )
        assert len(response.data) == 0

    def test_distance_in_response(self, client_app, active_specialist):
        response = client_app.get(
            SPECIALISTS_URL,
            {'lat': '55.796', 'lon': '49.106'},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data[0]['distance_km'] is not None
        assert response.data[0]['distance_km'] < 1.0

    def test_ordering_by_rating(self, client_app, active_specialist, db):
        user2 = User.objects.create_user(
            username='spec_low', password='pass',
            role='specialist', phone='+79990500030',
        )
        p2 = user2.specialist_profile
        p2.display_name = 'Новичок'
        p2.status = 'active'
        p2.rating = Decimal('3.0')
        p2.save()

        response = client_app.get(
            SPECIALISTS_URL, {'ordering': '-rating'},
        )
        assert response.data[0]['display_name'] == 'Елена Мастер'
        assert response.data[1]['display_name'] == 'Новичок'

    def test_unauthenticated_denied(self):
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(SPECIALISTS_URL)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_readonly(self, client_app):
        response = client_app.post(
            SPECIALISTS_URL, {'name': 'Hack'}, format='json',
        )
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
