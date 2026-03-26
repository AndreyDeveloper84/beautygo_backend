"""Fixtures for appointments tests."""
import pytest
from django.utils import timezone

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Test Category appt', slug='test-cat-appt')


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='specialist_appt', password='pass', role='specialist',
        phone='+79990000010',
    )


@pytest.fixture
def specialist(db, specialist_user):
    # Signal auto-creates SpecialistProfile on user save; just update it.
    profile = SpecialistProfile.objects.get(user=specialist_user)
    profile.display_name = 'Test Specialist Appt'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


@pytest.fixture
def service(db, specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name='Test Service',
        price='1500.00',
        duration_minutes=60,
        is_active=True,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='client_appt', password='pass', role='client',
        phone='+79990000011',
    )


@pytest.fixture
def future_dt():
    """A datetime 2 hours from now."""
    return timezone.now() + timezone.timedelta(hours=2)


@pytest.fixture
def appointment(db, client_user, specialist, service, future_dt):
    """Create appointment with confirmed status (cancellable by policy)."""
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist,
        service=service,
        start_datetime=future_dt,
        end_datetime=future_dt + timezone.timedelta(minutes=service.duration_minutes),
        price=service.price,
        status=Appointment.Status.CONFIRMED,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=service.duration_minutes,
    )
