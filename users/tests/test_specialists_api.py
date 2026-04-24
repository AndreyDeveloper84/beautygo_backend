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
        specialist=profile, name='Маникюр',
        price=Decimal('1500'), duration_minutes=60,
    )
    Service.objects.create(
        specialist=profile, name='Педикюр',
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
            specialist=active_specialist.specialist_profile,
            name='Маникюр',
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


@pytest.mark.django_db
class TestSpecialistDetail:
    """Tests for GET /api/v1/specialists/{id}/ (DRF-61)."""

    def test_retrieve_specialist(self, client_app, active_specialist):
        profile = active_specialist.specialist_profile
        url = f'{SPECIALISTS_URL}{profile.pk}/'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['display_name'] == 'Елена Мастер'
        assert 'services' in response.data
        assert 'reviews_summary' in response.data
        assert 'working_hours' in response.data
        assert 'recent_reviews' in response.data
        assert 'created_at' in response.data

    def test_retrieve_includes_full_services(
        self, client_app, active_specialist,
    ):
        profile = active_specialist.specialist_profile
        url = f'{SPECIALISTS_URL}{profile.pk}/'
        response = client_app.get(url)
        services = response.data['services']
        assert len(services) == 2
        # Full service has description and category_name
        assert 'description' in services[0]
        assert 'category_name' in services[0]
        assert 'duration_minutes' in services[0]

    def test_retrieve_reviews_summary_stub(
        self, client_app, active_specialist,
    ):
        profile = active_specialist.specialist_profile
        url = f'{SPECIALISTS_URL}{profile.pk}/'
        response = client_app.get(url)
        summary = response.data['reviews_summary']
        assert summary['average'] == 4.8
        assert summary['count'] == 25
        assert 'distribution' in summary

    def test_retrieve_nonexistent(self, client_app):
        import uuid
        url = f'{SPECIALISTS_URL}{uuid.uuid4()}/'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_inactive_specialist(
        self, client_app, draft_specialist,
    ):
        profile = draft_specialist.specialist_profile
        url = f'{SPECIALISTS_URL}{profile.pk}/'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_services_endpoint(self, client_app, active_specialist):
        profile = active_specialist.specialist_profile
        url = f'{SPECIALISTS_URL}{profile.pk}/services/'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 2
        assert 'category_name' in response.data[0]

    def test_slots_endpoint_requires_service_id(self, client_app, active_specialist):
        profile = active_specialist.specialist_profile
        url = f'{SPECIALISTS_URL}{profile.pk}/slots/'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_slots_endpoint_with_service(self, client_app, active_specialist):
        from datetime import time as dt_time, timedelta as dt_timedelta

        from django.utils import timezone

        from appointments.models import SpecialistWorkingHours

        profile = active_specialist.specialist_profile
        # Seed working hours 09:00-18:00 every day so slots can be generated.
        for day_of_week in range(7):
            SpecialistWorkingHours.objects.create(
                specialist=profile,
                day_of_week=day_of_week,
                is_working_day=True,
                start_time=dt_time(9, 0),
                end_time=dt_time(18, 0),
            )

        service = profile.services.first()
        tomorrow = (timezone.localdate() + dt_timedelta(days=1)).isoformat()
        url = f'{SPECIALISTS_URL}{profile.pk}/slots/?service_id={service.pk}&date={tomorrow}'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert 'slots' in response.data
        assert len(response.data['slots']) > 0

    def test_reviews_endpoint_stub(self, client_app, active_specialist):
        profile = active_specialist.specialist_profile
        url = f'{SPECIALISTS_URL}{profile.pk}/reviews/'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data'] == []
        assert response.data['meta']['count'] == 0


@pytest.mark.django_db
class TestSlotsRespectsWorkingHours:
    """Regression tests for ln-624 #1 — /specialists/{id}/slots/ must
    delegate to AvailabilityQueryService so SpecialistWorkingHours and
    SpecialistTimeOff are honoured (not hardcoded 09:00-18:00)."""

    def _next_weekday(self, weekday: int):
        """Return the next calendar date falling on the given weekday (0=Mon)."""
        from datetime import timedelta as dt_timedelta
        from django.utils import timezone
        today = timezone.localdate()
        delta = (weekday - today.weekday()) % 7 or 7
        return today + dt_timedelta(days=delta)

    def test_slots_respect_custom_working_hours(
        self, client_app, active_specialist,
    ):
        """A specialist who works 14:00-20:00 must not get 09:00-13:30 slots."""
        from datetime import time as dt_time
        from appointments.models import SpecialistWorkingHours

        profile = active_specialist.specialist_profile
        for day_of_week in range(7):
            SpecialistWorkingHours.objects.create(
                specialist=profile,
                day_of_week=day_of_week,
                is_working_day=True,
                start_time=dt_time(14, 0),
                end_time=dt_time(20, 0),
            )

        service = profile.services.first()
        target_date = self._next_weekday(0).isoformat()  # a Monday
        url = (
            f'{SPECIALISTS_URL}{profile.pk}/slots/'
            f'?service_id={service.pk}&date={target_date}'
        )
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK

        slots = response.data['slots']
        assert len(slots) > 0
        # No slot may start before 14:00 specialist-local.
        for slot_iso in slots:
            hour = int(slot_iso[11:13])
            assert hour >= 14, f"slot {slot_iso} violates 14:00 start time"

    def test_slots_empty_on_non_working_day(
        self, client_app, active_specialist,
    ):
        """Sunday off → /slots/ returns [] for a Sunday, not 18 fantom slots."""
        from datetime import time as dt_time
        from appointments.models import SpecialistWorkingHours

        profile = active_specialist.specialist_profile
        # Working Mon-Sat 10-18, Sunday off (no row created).
        for day_of_week in range(6):
            SpecialistWorkingHours.objects.create(
                specialist=profile,
                day_of_week=day_of_week,
                is_working_day=True,
                start_time=dt_time(10, 0),
                end_time=dt_time(18, 0),
            )

        service = profile.services.first()
        sunday = self._next_weekday(6).isoformat()
        url = (
            f'{SPECIALISTS_URL}{profile.pk}/slots/'
            f'?service_id={service.pk}&date={sunday}'
        )
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['slots'] == []

    def test_slots_exclude_time_off_interval(
        self, client_app, active_specialist,
    ):
        """An active SpecialistTimeOff overlapping the day must remove slots."""
        from datetime import datetime as dt_datetime, time as dt_time, timezone as dt_tz
        from zoneinfo import ZoneInfo
        from appointments.models import SpecialistTimeOff, SpecialistWorkingHours

        profile = active_specialist.specialist_profile
        for day_of_week in range(7):
            SpecialistWorkingHours.objects.create(
                specialist=profile,
                day_of_week=day_of_week,
                is_working_day=True,
                start_time=dt_time(9, 0),
                end_time=dt_time(18, 0),
            )

        tz = ZoneInfo(profile.timezone)
        target = self._next_weekday(0)  # any working day
        # Time-off 12:00-14:00 specialist-local on that day.
        SpecialistTimeOff.objects.create(
            specialist=profile,
            start_at=dt_datetime.combine(target, dt_time(12, 0), tzinfo=tz).astimezone(dt_tz.utc),
            end_at=dt_datetime.combine(target, dt_time(14, 0), tzinfo=tz).astimezone(dt_tz.utc),
            reason='lunch',
        )

        service = profile.services.first()
        url = (
            f'{SPECIALISTS_URL}{profile.pk}/slots/'
            f'?service_id={service.pk}&date={target.isoformat()}'
        )
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK

        slots = response.data['slots']
        assert len(slots) > 0
        # No slot start may fall inside 12:00-14:00.
        for slot_iso in slots:
            hour = int(slot_iso[11:13])
            minute = int(slot_iso[14:16])
            minutes_of_day = hour * 60 + minute
            assert not (720 <= minutes_of_day < 840), (
                f"slot {slot_iso} falls inside 12:00-14:00 time-off"
            )
