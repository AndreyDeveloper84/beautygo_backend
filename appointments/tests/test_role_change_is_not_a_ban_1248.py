"""Смена роли — не бан: погашенная строка рядом с активной не запрещает запись (DRF-1248).

Решение владельца OD-6 (21.08): ``has_revoked`` обязан отличать смену роли
от бана. ``tur_unique_active`` держит не больше одной АКТИВНОЙ связи на пару
(человек, салон), роли взаимоисключающие — поэтому апгрейд ``customer →
admin`` двухшаговый: старую погасить, новую создать. До этой правки
``create_booking_service`` отказывал на ЛЮБОЙ неактивной строке, не глядя на
активную рядом: владелец, ставший администратором своего салона, терял
возможность в нём записаться, а отказ приходил как «мастера не существует».

Правило после: отказ только когда погашенные есть, а активной — нет (бан).
Активная строка и есть то «явное действие администратора», о котором
говорит комментарий F2: создать её может только администратор.

Второе место с той же проверкой — мост ``users/signals.py`` (``user.role`` +
``user.tenant`` → TUR): приведён к тому же правилу, иначе «отозван» значило
бы разное в двух местах.
"""

from __future__ import annotations

import logging

import pytest
from django.utils import timezone as dj_tz
from rest_framework.exceptions import NotFound

from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.models import Appointment
from appointments.tests.test_grant_on_first_booking_1014 import (
    _dto,
    _make_service,
    _make_specialist,
)
from tenants.models import Tenant
from users.models import TenantUserRelationship, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="r1248-t", name="R1248 Tenant")


@pytest.fixture
def person():
    return User.objects.create_user(
        username="r1248_owner", password="x", role="client",  # pragma: allowlist secret
        phone="+79991248001",
    )


def _retire_customer_and_grant_admin(person, tenant) -> None:
    """The two-step upgrade `tur_unique_active` forces on the admin surface."""
    TenantUserRelationship.objects.create(
        user=person, tenant=tenant, is_active=False,
        role=TenantUserRelationship.Role.CUSTOMER,
        revoked_at=dj_tz.now(), revoke_reason="role_change",
    )
    TenantUserRelationship.objects.create(
        user=person, tenant=tenant, is_active=True,
        role=TenantUserRelationship.Role.ADMIN,
    )


class TestBookingDistinguishesRoleChangeFromBan:
    def test_a_person_upgraded_to_admin_can_still_book(self, person, tenant):
        """Красный до правки: NotFound «Specialist not found.» на погашенной customer-строке."""
        _retire_customer_and_grant_admin(person, tenant)
        specialist = _make_specialist(tenant, suffix="4801")
        service = _make_service(specialist)

        appointment = CreateBookingService().execute(_dto(person, specialist, service))

        assert appointment is not None
        assert Appointment.objects.filter(client_id=person.id).count() == 1
        # No third row: the active admin relationship IS the grant.
        assert TenantUserRelationship.objects.filter(user=person, tenant=tenant).count() == 2
        assert TenantUserRelationship.objects.filter(
            user=person, tenant=tenant, is_active=True,
        ).count() == 1

    def test_a_ban_still_refuses(self, person, tenant):
        """Регрессия, которую легче всего потерять: погашенная без активной — отказ, как F2 (#152)."""
        TenantUserRelationship.objects.create(
            user=person, tenant=tenant, is_active=False,
            revoked_at=dj_tz.now(), revoke_reason="admin_ban",
        )
        specialist = _make_specialist(tenant, suffix="4802")
        service = _make_service(specialist)

        with pytest.raises(NotFound):
            CreateBookingService().execute(_dto(person, specialist, service))

        assert Appointment.objects.count() == 0
        assert not TenantUserRelationship.objects.filter(
            user=person, tenant=tenant, is_active=True,
        ).exists()

    def test_a_regrant_after_a_ban_is_an_explicit_admin_action(self, person, tenant):
        """Строка 3 таблицы OD-6: после бана администратор создал активную связь — запись проходит."""
        TenantUserRelationship.objects.create(
            user=person, tenant=tenant, is_active=False,
            revoked_at=dj_tz.now(), revoke_reason="admin_ban",
        )
        TenantUserRelationship.objects.create(
            user=person, tenant=tenant, is_active=True,
            role=TenantUserRelationship.Role.CUSTOMER,
        )
        specialist = _make_specialist(tenant, suffix="4803")
        service = _make_service(specialist)

        CreateBookingService().execute(_dto(person, specialist, service))

        assert Appointment.objects.filter(client_id=person.id).count() == 1


@pytest.fixture
def signals_log(caplog):
    """``users`` logger is ``propagate: False`` (settings LOGGING) — caplog's
    root handler never sees ``users.signals``. Attach it directly."""
    target = logging.getLogger("users.signals")
    target.addHandler(caplog.handler)
    previous = target.level
    target.setLevel(logging.WARNING)
    try:
        yield caplog
    finally:
        target.removeHandler(caplog.handler)
        target.setLevel(previous)


class TestTheUserTenantBridgeUsesTheSameRule:
    def test_bridge_is_not_skipped_when_an_active_row_exists(self, person, tenant, signals_log):
        """users/signals.py: погашенная рядом с активной — не «отозван», мост не пропускается молча.

        Красный до правки: WARNING «bridge skipped: revoked TUR exists».
        Поведение по данным то же (активная уже есть, get_or_create её и
        находит), но лог утверждал бан там, где была смена роли.
        """
        _retire_customer_and_grant_admin(person, tenant)
        person.tenant = tenant
        person.role = "admin"

        person.save()

        assert not any("bridge skipped" in r.getMessage() for r in signals_log.records), [
            r.getMessage() for r in signals_log.records
        ]
        assert TenantUserRelationship.objects.filter(
            user=person, tenant=tenant, is_active=True,
        ).count() == 1

    def test_bridge_is_still_skipped_for_a_ban(self, person, tenant, signals_log):
        """Контроль: бан без активной — мост пропускается с прежним WARNING, связь не воскресает."""
        TenantUserRelationship.objects.create(
            user=person, tenant=tenant, is_active=False,
            revoked_at=dj_tz.now(), revoke_reason="admin_ban",
        )
        person.tenant = tenant

        person.save()

        assert any("bridge skipped" in r.getMessage() for r in signals_log.records), [
            r.getMessage() for r in signals_log.records
        ]
        assert not TenantUserRelationship.objects.filter(
            user=person, tenant=tenant, is_active=True,
        ).exists()
