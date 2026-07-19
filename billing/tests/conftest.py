"""Fixtures for billing tests — mirrors appointments/tests/conftest.py style.

B-1 handoff: until W1 registers `billing` in INSTALLED_APPS these tests
run only under the W2 shim (--ds=billing.tests.settings_w2). Under the
canonical settings the whole directory is skipped at collection — the
canonical suite must stay green for the other streams TODAY, and
importing billing.models without the app installed raises RuntimeError.
"""
from datetime import date

import pytest
from django.conf import settings
from django.utils import timezone

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

if "billing" not in settings.INSTALLED_APPS:
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='billing_specialist', password='pass', role='specialist',
        phone='+79990000020',
    )


@pytest.fixture
def specialist(db, specialist_user):
    # Signal auto-creates SpecialistProfile on user save; just update it.
    profile = SpecialistProfile.objects.get(user=specialist_user)
    profile.display_name = 'Billing Specialist'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(name='Billing Salon', slug='billing-salon')


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Billing Category', slug='billing-cat')


@pytest.fixture
def service(db, specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name='Billing Service',
        price='1500.00',
        duration_minutes=60,
        is_active=True,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='billing_client', password='pass', role='client',
        phone='+79990000021',
    )


@pytest.fixture
def appointment(db, client_user, specialist, service):
    """Confirmed appointment; root conftest stamps tenant via pre_save."""
    start = timezone.now() + timezone.timedelta(hours=2)
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist,
        service=service,
        start_datetime=start,
        end_datetime=start + timezone.timedelta(minutes=service.duration_minutes),
        price=service.price,
        status=Appointment.Status.CONFIRMED,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=service.duration_minutes,
    )


@pytest.fixture
def tariff_solo(db):
    # Seeded by billing/migrations/0002_seed_tariff_plans.py.
    from billing.models import TariffPlan

    return TariffPlan.objects.get(code=TariffPlan.Code.SOLO)


@pytest.fixture
def tariff_salon(db):
    from billing.models import TariffPlan

    return TariffPlan.objects.get(code=TariffPlan.Code.SALON)


@pytest.fixture
def subscription(db, specialist_user, tariff_solo):
    from billing.models import SpecialistSubscription

    return SpecialistSubscription.objects.create(
        user=specialist_user,
        tariff=tariff_solo,
        status=SpecialistSubscription.Status.ACTIVE,
        current_period_start=date(2026, 7, 1),
        current_period_end=date(2026, 7, 31),
    )


@pytest.fixture
def salon_subscription(db, specialist_user, tenant, tariff_salon):
    from billing.models import SpecialistSubscription

    return SpecialistSubscription.objects.create(
        user=specialist_user,
        tenant=tenant,
        tariff=tariff_salon,
        status=SpecialistSubscription.Status.ACTIVE,
        current_period_start=date(2026, 7, 1),
        current_period_end=date(2026, 7, 31),
    )


@pytest.fixture
def subscription_with_card(db, subscription):
    subscription.payment_method_id = "pm_test_123"
    subscription.save(update_fields=["payment_method_id"])
    return subscription


@pytest.fixture
def due_subscription(db, subscription_with_card):
    """Active subscription whose paid period has ended (charge is due)."""
    subscription_with_card.current_period_start = date(2026, 6, 11)
    subscription_with_card.current_period_end = date(2026, 7, 10)
    subscription_with_card.save(
        update_fields=["current_period_start", "current_period_end"],
    )
    return subscription_with_card
