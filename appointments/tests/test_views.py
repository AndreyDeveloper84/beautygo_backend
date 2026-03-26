"""Tests for appointments API endpoints."""
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from appointments.models import Appointment


@pytest.mark.django_db
class TestAppointmentCreate:
    """POST /api/v1/appointments/"""

    def _client(self, user):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=user)
        return c

    def test_create_success(self, client_user, specialist, service):
        start = (timezone.now() + timezone.timedelta(hours=3)).replace(
            second=0, microsecond=0,
        )
        response = self._client(client_user).post(
            '/api/v1/appointments/',
            data={
                'specialist_id': str(specialist.id),
                'service_id': str(service.id),
                'start_datetime': start.isoformat(),
            },
            format='json',
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert Appointment.objects.count() == 1
        appt = Appointment.objects.first()
        assert appt.status == Appointment.Status.AWAITING_PAYMENT
        assert appt.client == client_user
        assert appt.snapshot_service_name == service.name
        assert float(appt.snapshot_price) == float(service.price)

    def test_create_unauthenticated(self, specialist, service):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        start = (timezone.now() + timezone.timedelta(hours=3)).isoformat()
        response = c.post(
            '/api/v1/appointments/',
            data={
                'specialist_id': str(specialist.id),
                'service_id': str(service.id),
                'start_datetime': start,
            },
            format='json',
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_create_too_soon(self, client_user, specialist, service):
        """Cannot book less than 1 hour in advance."""
        start = (timezone.now() + timezone.timedelta(minutes=30)).isoformat()
        response = self._client(client_user).post(
            '/api/v1/appointments/',
            data={
                'specialist_id': str(specialist.id),
                'service_id': str(service.id),
                'start_datetime': start,
            },
            format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_slot_taken(self, client_user, specialist, service, appointment):
        """Cannot double-book the same time slot."""
        overlap_start = appointment.start_datetime + timezone.timedelta(minutes=30)
        response = self._client(client_user).post(
            '/api/v1/appointments/',
            data={
                'specialist_id': str(specialist.id),
                'service_id': str(service.id),
                'start_datetime': overlap_start.isoformat(),
            },
            format='json',
        )
        # Slot conflict returns 409 from booking engine
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_create_specialist_not_found(self, client_user, service):
        import uuid
        start = (timezone.now() + timezone.timedelta(hours=3)).isoformat()
        response = self._client(client_user).post(
            '/api/v1/appointments/',
            data={
                'specialist_id': str(uuid.uuid4()),
                'service_id': str(service.id),
                'start_datetime': start,
            },
            format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_specialist_cannot_create(self, specialist_user, specialist, service):
        """Specialists cannot create appointments through the client endpoint."""
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'pro'
        c.force_authenticate(user=specialist_user)
        start = (timezone.now() + timezone.timedelta(hours=3)).isoformat()
        response = c.post(
            '/api/v1/appointments/',
            data={
                'specialist_id': str(specialist.id),
                'service_id': str(service.id),
                'start_datetime': start,
            },
            format='json',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestAppointmentList:
    """GET /api/v1/appointments/"""

    def test_client_sees_own_appointments(self, client_user, appointment):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.get('/api/v1/appointments/')
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['data']) == 1

    def test_client_does_not_see_others(self, appointment):
        other = __import__('users.models', fromlist=['User']).User.objects.create_user(
            username='other_client', password='pass', role='client',
            phone='+79990000099',
        )
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=other)
        response = c.get('/api/v1/appointments/')
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['data']) == 0

    def test_specialist_sees_own_appointments(self, specialist_user, appointment):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'pro'
        c.force_authenticate(user=specialist_user)
        response = c.get('/api/v1/appointments/')
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['data']) == 1

    def test_filter_by_status(self, client_user, appointment):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.get('/api/v1/appointments/?status=cancelled')
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['data']) == 0


@pytest.mark.django_db
class TestAppointmentDetail:
    """GET /api/v1/appointments/{id}/"""

    def test_client_can_retrieve_own(self, client_user, appointment):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.get(f'/api/v1/appointments/{appointment.id}/')
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['id'] == str(appointment.id)

    def test_other_user_cannot_retrieve(self, appointment):
        from users.models import User
        other = User.objects.create_user(
            username='stranger_appt', password='pass', role='client',
            phone='+79990000098',
        )
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=other)
        response = c.get(f'/api/v1/appointments/{appointment.id}/')
        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
class TestAppointmentCancel:
    """POST /api/v1/appointments/{id}/cancel/"""

    def test_client_can_cancel(self, client_user, appointment):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.post(
            f'/api/v1/appointments/{appointment.id}/cancel/',
            data={'reason': 'Changed my mind'},
            format='json',
        )
        assert response.status_code == status.HTTP_200_OK
        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.CANCELLED
        assert appointment.cancellation_reason == 'Changed my mind'

    def test_specialist_can_cancel(self, specialist_user, appointment):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'pro'
        c.force_authenticate(user=specialist_user)
        response = c.post(
            f'/api/v1/appointments/{appointment.id}/cancel/',
            data={},
            format='json',
        )
        assert response.status_code == status.HTTP_200_OK
        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.CANCELLED

    def test_cannot_cancel_completed(self, client_user, appointment):
        appointment.status = Appointment.Status.COMPLETED
        appointment.save(update_fields=['status'])
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.post(
            f'/api/v1/appointments/{appointment.id}/cancel/',
            data={},
            format='json',
        )
        assert response.status_code == 422


@pytest.mark.django_db
class TestAppointmentComplete:
    """POST /api/v1/appointments/{id}/complete/"""

    def _confirm(self, appointment):
        appointment.status = Appointment.Status.CONFIRMED
        appointment.save(update_fields=['status'])

    def test_specialist_can_complete(self, specialist_user, appointment):
        self._confirm(appointment)
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'pro'
        c.force_authenticate(user=specialist_user)
        response = c.post(f'/api/v1/appointments/{appointment.id}/complete/')
        assert response.status_code == status.HTTP_200_OK
        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.COMPLETED

    def test_client_cannot_complete(self, client_user, appointment):
        self._confirm(appointment)
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.post(f'/api/v1/appointments/{appointment.id}/complete/')
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_cannot_complete_cancelled(self, specialist_user, appointment):
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=['status'])
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'pro'
        c.force_authenticate(user=specialist_user)
        response = c.post(f'/api/v1/appointments/{appointment.id}/complete/')
        assert response.status_code == 422


@pytest.mark.django_db
class TestSlotsEndpoint:
    """GET /api/v1/specialists/{id}/slots/"""

    def test_requires_service_id(self, client_user, specialist):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.get(f'/api/v1/specialists/{specialist.id}/slots/')
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_returns_slots(self, client_user, specialist, service):
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        tomorrow = (timezone.localdate() + timezone.timedelta(days=1)).isoformat()
        response = c.get(
            f'/api/v1/specialists/{specialist.id}/slots/'
            f'?service_id={service.id}&date={tomorrow}',
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert 'slots' in data
        assert isinstance(data['slots'], list)
        # Tomorrow should have slots (working hours 09:00-18:00, 60-min service)
        assert len(data['slots']) > 0

    def test_booked_slot_excluded(self, client_user, specialist, service, appointment):
        """A slot that is already booked should not appear."""
        slot_date = appointment.start_datetime.date().isoformat()
        c = APIClient()
        c.defaults['HTTP_X_APP_TYPE'] = 'client'
        c.force_authenticate(user=client_user)
        response = c.get(
            f'/api/v1/specialists/{specialist.id}/slots/'
            f'?service_id={service.id}&date={slot_date}',
        )
        assert response.status_code == status.HTTP_200_OK
        slots = response.data['slots']
        # The booked slot should not be in the list
        booked_iso = appointment.start_datetime.isoformat()
        assert booked_iso not in slots
