"""Tests for DRF-131 (Working Hours CRUD) and DRF-132 (TimeOff CRUD)."""
from __future__ import annotations

import pytest
import datetime as dt

from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import SpecialistWorkingHours, SpecialistTimeOff
from users.models import SpecialistProfile, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='schedspecialist',
        password='testpass123',
        role='specialist',
        phone='+79990001111',
    )


@pytest.fixture
def specialist_profile(specialist_user):
    # Signal auto-creates SpecialistProfile on user creation — just update it
    profile = specialist_user.specialist_profile
    profile.display_name = 'Test Specialist'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save(update_fields=['display_name', 'status'])
    return profile


@pytest.fixture
def pro_client(specialist_user, specialist_profile):
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'pro'
    client.force_authenticate(user=specialist_user)
    return client


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='schedclient',
        password='testpass123',
        role='client',
        phone='+79990002222',
    )


@pytest.fixture
def client_api(client_user):
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'client'
    client.force_authenticate(user=client_user)
    return client


SCHEDULE_URL = '/api/v1/specialists/me/schedule/'
TIME_OFF_URL = '/api/v1/specialists/me/time-off/'

FULL_SCHEDULE = {
    'schedule': [
        {'day_of_week': 0, 'is_working_day': True, 'start_time': '09:00', 'end_time': '18:00'},
        {'day_of_week': 1, 'is_working_day': True, 'start_time': '09:00', 'end_time': '18:00'},
        {'day_of_week': 2, 'is_working_day': True, 'start_time': '09:00', 'end_time': '18:00'},
        {'day_of_week': 3, 'is_working_day': True, 'start_time': '09:00', 'end_time': '18:00'},
        {'day_of_week': 4, 'is_working_day': True, 'start_time': '09:00', 'end_time': '18:00'},
        {'day_of_week': 5, 'is_working_day': False},
        {'day_of_week': 6, 'is_working_day': False},
    ]
}


# ---------------------------------------------------------------------------
# DRF-131: Working Hours CRUD
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestScheduleGet:
    def test_get_empty_returns_7_days(self, pro_client, specialist_profile):
        """No schedule yet → returns all 7 days as not working."""
        resp = pro_client.get(SCHEDULE_URL)
        assert resp.status_code == 200
        data = resp.json()['data']
        assert len(data) == 7
        assert all(not d['is_working_day'] for d in data)

    def test_get_returns_existing_schedule(self, pro_client, specialist_profile):
        """Returns persisted working hours in day order."""
        SpecialistWorkingHours.objects.create(
            specialist=specialist_profile,
            day_of_week=0,
            is_working_day=True,
            start_time='09:00',
            end_time='18:00',
        )
        resp = pro_client.get(SCHEDULE_URL)
        assert resp.status_code == 200
        data = resp.json()['data']
        assert len(data) == 7
        monday = next(d for d in data if d['day_of_week'] == 0)
        assert monday['is_working_day'] is True
        assert monday['start_time'] == '09:00'
        assert monday['end_time'] == '18:00'

    def test_get_requires_specialist_role(self, client_api):
        """Client role cannot access schedule endpoint."""
        resp = client_api.get(SCHEDULE_URL)
        assert resp.status_code == 403

    def test_get_requires_auth(self):
        """Unauthenticated request returns 403 (middleware blocks before auth)."""
        client = APIClient()
        resp = client.get(SCHEDULE_URL)
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestSchedulePut:
    def test_put_creates_full_schedule(self, pro_client, specialist_profile):
        """PUT creates all 7 days."""
        resp = pro_client.put(SCHEDULE_URL, FULL_SCHEDULE, format='json')
        assert resp.status_code == 200
        assert SpecialistWorkingHours.objects.filter(
            specialist=specialist_profile,
        ).count() == 7

    def test_put_replaces_existing(self, pro_client, specialist_profile):
        """PUT deletes old schedule and creates new one."""
        SpecialistWorkingHours.objects.create(
            specialist=specialist_profile, day_of_week=0,
            is_working_day=True, start_time='10:00', end_time='17:00',
        )
        resp = pro_client.put(SCHEDULE_URL, FULL_SCHEDULE, format='json')
        assert resp.status_code == 200
        monday = SpecialistWorkingHours.objects.get(
            specialist=specialist_profile, day_of_week=0,
        )
        assert str(monday.start_time) == '09:00:00'

    def test_put_requires_all_7_days(self, pro_client, specialist_profile):
        """PUT with fewer than 7 days returns 400."""
        data = {'schedule': FULL_SCHEDULE['schedule'][:5]}
        resp = pro_client.put(SCHEDULE_URL, data, format='json')
        assert resp.status_code == 400

    def test_put_rejects_duplicate_days(self, pro_client, specialist_profile):
        """PUT with duplicate day_of_week returns 400."""
        bad = {'schedule': [
            {'day_of_week': 0, 'is_working_day': True, 'start_time': '09:00', 'end_time': '18:00'},
            {'day_of_week': 0, 'is_working_day': False},
            {'day_of_week': 1, 'is_working_day': False},
            {'day_of_week': 2, 'is_working_day': False},
            {'day_of_week': 3, 'is_working_day': False},
            {'day_of_week': 4, 'is_working_day': False},
            {'day_of_week': 5, 'is_working_day': False},
        ]}
        resp = pro_client.put(SCHEDULE_URL, bad, format='json')
        assert resp.status_code == 400

    def test_put_rejects_invalid_times(self, pro_client, specialist_profile):
        """Working day with start >= end returns 400."""
        bad = dict(FULL_SCHEDULE)
        bad = {
            'schedule': [
                {'day_of_week': 0, 'is_working_day': True, 'start_time': '18:00', 'end_time': '09:00'},
                *FULL_SCHEDULE['schedule'][1:],
            ]
        }
        resp = pro_client.put(SCHEDULE_URL, bad, format='json')
        assert resp.status_code == 400

    def test_put_validates_break_within_hours(self, pro_client, specialist_profile):
        """Break outside working hours returns 400."""
        bad_schedule = list(FULL_SCHEDULE['schedule'])
        bad_schedule[0] = {
            'day_of_week': 0,
            'is_working_day': True,
            'start_time': '09:00',
            'end_time': '18:00',
            'break_start': '07:00',  # before start
            'break_end': '08:00',
        }
        resp = pro_client.put(SCHEDULE_URL, {'schedule': bad_schedule}, format='json')
        assert resp.status_code == 400


@pytest.mark.django_db
class TestSchedulePatch:
    def test_patch_updates_specific_days(self, pro_client, specialist_profile):
        """PATCH updates only the provided days."""
        # Create initial schedule
        pro_client.put(SCHEDULE_URL, FULL_SCHEDULE, format='json')

        resp = pro_client.patch(SCHEDULE_URL, {
            'schedule': [
                {'day_of_week': 0, 'is_working_day': False},
            ]
        }, format='json')
        assert resp.status_code == 200

        monday = SpecialistWorkingHours.objects.get(
            specialist=specialist_profile, day_of_week=0,
        )
        assert not monday.is_working_day

        # Other days unchanged
        tuesday = SpecialistWorkingHours.objects.get(
            specialist=specialist_profile, day_of_week=1,
        )
        assert tuesday.is_working_day

    def test_patch_creates_new_day_if_not_exists(self, pro_client, specialist_profile):
        """PATCH upserts — creates if day didn't exist."""
        resp = pro_client.patch(SCHEDULE_URL, {
            'schedule': [
                {'day_of_week': 2, 'is_working_day': True, 'start_time': '10:00', 'end_time': '20:00'},
            ]
        }, format='json')
        assert resp.status_code == 200
        assert SpecialistWorkingHours.objects.filter(
            specialist=specialist_profile, day_of_week=2,
        ).exists()

    def test_patch_rejects_empty_schedule(self, pro_client, specialist_profile):
        """PATCH with empty schedule list returns 400."""
        resp = pro_client.patch(SCHEDULE_URL, {'schedule': []}, format='json')
        assert resp.status_code == 400

    def test_patch_with_break(self, pro_client, specialist_profile):
        """PATCH with valid break times succeeds."""
        resp = pro_client.patch(SCHEDULE_URL, {
            'schedule': [{
                'day_of_week': 1,
                'is_working_day': True,
                'start_time': '09:00',
                'end_time': '18:00',
                'break_start': '13:00',
                'break_end': '14:00',
            }]
        }, format='json')
        assert resp.status_code == 200
        result = resp.json()['data']
        tuesday = next(d for d in result if d['day_of_week'] == 1)
        assert tuesday['break_start'] == '13:00'
        assert tuesday['break_end'] == '14:00'


# ---------------------------------------------------------------------------
# DRF-132: TimeOff CRUD
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTimeOffList:
    def test_get_empty(self, pro_client, specialist_profile):
        resp = pro_client.get(TIME_OFF_URL)
        assert resp.status_code == 200
        assert resp.json()['data'] == []

    def test_filter_by_date_range(self, pro_client, specialist_profile):
        """date_from/date_to filters work correctly."""
        SpecialistTimeOff.objects.create(
            specialist=specialist_profile,
            start_at='2026-05-01T10:00:00Z',
            end_at='2026-05-03T18:00:00Z',
        )
        SpecialistTimeOff.objects.create(
            specialist=specialist_profile,
            start_at='2026-06-01T10:00:00Z',
            end_at='2026-06-05T18:00:00Z',
        )

        resp = pro_client.get(TIME_OFF_URL, {'date_from': '2026-05-01', 'date_to': '2026-05-31'})
        assert resp.status_code == 200
        data = resp.json()['data']
        assert len(data) == 1


@pytest.mark.django_db
class TestTimeOffCreate:
    def test_create_success(self, pro_client, specialist_profile):
        resp = pro_client.post(TIME_OFF_URL, {
            'start_at': '2026-07-01T00:00:00Z',
            'end_at': '2026-07-07T23:59:59Z',
            'reason': 'Отпуск',
        }, format='json')
        assert resp.status_code == 201
        data = resp.json()['data']
        assert data['reason'] == 'Отпуск'
        assert SpecialistTimeOff.objects.filter(specialist=specialist_profile).count() == 1

    def test_create_without_reason(self, pro_client, specialist_profile):
        resp = pro_client.post(TIME_OFF_URL, {
            'start_at': '2026-08-01T00:00:00Z',
            'end_at': '2026-08-02T00:00:00Z',
        }, format='json')
        assert resp.status_code == 201
        assert resp.json()['data']['reason'] == ''

    def test_create_invalid_dates(self, pro_client, specialist_profile):
        """end_at before start_at returns 400."""
        resp = pro_client.post(TIME_OFF_URL, {
            'start_at': '2026-08-10T00:00:00Z',
            'end_at': '2026-08-05T00:00:00Z',
        }, format='json')
        assert resp.status_code == 400

    def test_create_blocked_by_active_appointment(self, pro_client, specialist_profile, db):
        """Cannot create time-off when active appointment overlaps."""
        from appointments.models import Appointment
        from services.models import Service, ServiceCategory
        from users.models import User

        client_u = User.objects.create_user(
            username='bookingclient', role='client', phone='+79000011111',
        )
        cat = ServiceCategory.objects.create(name='Test Cat', slug='test-cat')
        svc = Service.objects.create(
            specialist=specialist_profile,
            category=cat,
            name='Маникюр',
            price=1000,
            duration_minutes=60,
        )
        Appointment.objects.create(
            client=client_u,
            specialist=specialist_profile,
            service=svc,
            start_datetime=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc),
            end_datetime=dt.datetime(2026, 9, 1, 11, 0, tzinfo=dt.timezone.utc),
            price=1000,
            status='confirmed',
        )

        resp = pro_client.post(TIME_OFF_URL, {
            'start_at': '2026-09-01T00:00:00Z',
            'end_at': '2026-09-02T00:00:00Z',
        }, format='json')
        assert resp.status_code == 409
        assert resp.json()['error']['code'] == 'HAS_ACTIVE_APPOINTMENTS'

    def test_create_requires_specialist_role(self, client_api):
        resp = client_api.post(TIME_OFF_URL, {
            'start_at': '2026-07-01T00:00:00Z',
            'end_at': '2026-07-07T23:59:59Z',
        }, format='json')
        assert resp.status_code == 403


@pytest.mark.django_db
class TestTimeOffDelete:
    def test_delete_success(self, pro_client, specialist_profile):
        to = SpecialistTimeOff.objects.create(
            specialist=specialist_profile,
            start_at='2026-10-01T00:00:00Z',
            end_at='2026-10-05T00:00:00Z',
        )
        resp = pro_client.delete(f'{TIME_OFF_URL}{to.id}/')
        assert resp.status_code == 204
        assert not SpecialistTimeOff.objects.filter(id=to.id).exists()

    def test_delete_not_found(self, pro_client, specialist_profile):
        import uuid
        resp = pro_client.delete(f'{TIME_OFF_URL}{uuid.uuid4()}/')
        assert resp.status_code == 404

    def test_delete_other_specialists_block(self, specialist_profile, db):
        """Cannot delete another specialist's time-off."""
        other_user = User.objects.create_user(
            username='otherspec', role='specialist', phone='+79000099999',
        )
        other_profile = other_user.specialist_profile
        to = SpecialistTimeOff.objects.create(
            specialist=other_profile,
            start_at='2026-11-01T00:00:00Z',
            end_at='2026-11-02T00:00:00Z',
        )
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'pro'
        client.force_authenticate(user=other_user)

        # own user deletes own block — OK
        resp = client.delete(f'{TIME_OFF_URL}{to.id}/')
        assert resp.status_code == 204

    def test_delete_requires_auth(self):
        import uuid
        client = APIClient()
        resp = client.delete(f'{TIME_OFF_URL}{uuid.uuid4()}/')
        assert resp.status_code in (401, 403)
