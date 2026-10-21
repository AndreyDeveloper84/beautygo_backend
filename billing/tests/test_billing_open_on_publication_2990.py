"""DRF-2990 (В-10, вариант А) — каталог открывает подписку на публикации.

* модерация → ACTIVE открывает строку тенанта (соло и салон) и в ту же
  минуту стартует пробный месяц — мастер виден клиентам;
* соло ``publish()`` (201, PENDING) открывает строку, пробный НЕ стартует;
* повторная публикация и второй мастер — no-op: одна строка, одно событие;
* без ``BILLING_ENABLED`` ничего не открывается;
* сбой биллинга не валит публикацию;
* ночной sweep открывает строку тенанту с мастером в продаже без подписки.
"""
from __future__ import annotations

import itertools
import logging
import uuid

import pytest
from django.utils import timezone

from appointments.models import OutboxEvent
from billing.charges import open_on_publication, start_pending_trials
from billing.models import SpecialistSubscription, TariffPlan
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.publication import publish
from users.tests.test_publication_m4_1796 import (
    OLGA,
    _approve,
    _make_ready,
    _provision,
)

pytestmark = pytest.mark.django_db

_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-runtime-internal-token-2990"  # noqa: S105


@pytest.fixture
def billing_on(settings):
    settings.BILLING_ENABLED = True
    settings.BILLING_CHARGES_ENABLED = False


@pytest.fixture
def operator(db) -> User:
    return User.objects.create_superuser(
        username="op-2990", password="pw", email="op-2990@x.y", role="admin",  # pragma: allowlist secret
    )


@pytest.fixture
def olga():
    return _provision(OLGA, "solo-max-2990olga")


def _salon(*, with_admin: bool = True) -> Tenant:
    n = next(_seq)
    salon = Tenant.objects.create(slug=f"salon-2990-{n}", name=f"Салон 2990 {n}")
    if with_admin:
        owner = User.objects.create_user(
            username=f"own_2990_{n}", password="x", role="client",  # noqa: S106
            phone=f"+79995{n:06d}",
        )
        TenantUserRelationship.objects.create(
            user=owner, tenant=salon, role=TenantUserRelationship.Role.ADMIN,
            is_active=True,
        )
    return salon


def _salon_master(salon: Tenant) -> SpecialistProfile:
    n = next(_seq)
    user = User.objects.create_user(
        username=f"m_2990_{n}", password="x", role="specialist",  # noqa: S106
        phone=f"+79996{n:06d}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.status = SpecialistProfile.ProfileStatus.PENDING
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


def _trial_events() -> list[dict]:
    return [
        row.payload["data"]
        for row in OutboxEvent.objects.filter(topic="subscription.trial_started")
    ]


def _approve_on_commit(profile, operator, capture):
    with capture(execute=True):
        _approve(SpecialistProfile.objects.filter(pk=profile.pk), operator)


class TestModerationPublishes:
    def test_salon_master_approved_opens_salon_account_and_starts_trial(
        self, billing_on, operator, django_capture_on_commit_callbacks,
    ):
        salon = _salon()
        master = _salon_master(salon)

        _approve_on_commit(master, operator, django_capture_on_commit_callbacks)

        sub = SpecialistSubscription.objects.get(tenant=salon)
        assert sub.tariff == TariffPlan.objects.get(code="salon")
        assert sub.status == SpecialistSubscription.Status.TRIAL
        assert sub.current_period_start == timezone.localdate()
        assert sub.trial_ends_at is not None
        assert [e["tenant_id"] for e in _trial_events()] == [str(salon.pk)]

    def test_second_master_is_a_no_op(
        self, billing_on, operator, django_capture_on_commit_callbacks,
    ):
        salon = _salon()
        first, second = _salon_master(salon), _salon_master(salon)
        _approve_on_commit(first, operator, django_capture_on_commit_callbacks)
        trial_ends_at = SpecialistSubscription.objects.get(tenant=salon).trial_ends_at

        _approve_on_commit(second, operator, django_capture_on_commit_callbacks)

        assert SpecialistSubscription.objects.filter(tenant=salon).count() == 1
        assert SpecialistSubscription.objects.get(tenant=salon).trial_ends_at == trial_ends_at
        assert len(_trial_events()) == 1

    def test_flag_off_opens_nothing(
        self, settings, operator, django_capture_on_commit_callbacks,
    ):
        settings.BILLING_ENABLED = False
        salon = _salon()
        master = _salon_master(salon)

        _approve_on_commit(master, operator, django_capture_on_commit_callbacks)

        master.refresh_from_db()
        assert master.status == SpecialistProfile.ProfileStatus.ACTIVE
        assert not SpecialistSubscription.objects.filter(tenant=salon).exists()

    def test_no_payer_does_not_fail_the_publication(
        self, billing_on, operator, django_capture_on_commit_callbacks, caplog,
    ):
        salon = _salon(with_admin=False)
        master = _salon_master(salon)

        with caplog.at_level(logging.ERROR, logger="billing.charges"):
            _approve_on_commit(master, operator, django_capture_on_commit_callbacks)

        master.refresh_from_db()
        assert master.status == SpecialistProfile.ProfileStatus.ACTIVE
        assert not SpecialistSubscription.objects.filter(tenant=salon).exists()
        assert "billing.open_on_publication.no_tenant_admin" in caplog.text

    def test_billing_crash_does_not_fail_the_publication(
        self, billing_on, operator, django_capture_on_commit_callbacks, monkeypatch, caplog,
    ):
        def boom(*a, **kw):
            raise RuntimeError("billing down")

        monkeypatch.setattr("billing.charges.ensure_tenant_subscription", boom)
        salon = _salon()
        master = _salon_master(salon)

        with caplog.at_level(logging.ERROR, logger="billing.charges"):
            _approve_on_commit(master, operator, django_capture_on_commit_callbacks)

        master.refresh_from_db()
        assert master.status == SpecialistProfile.ProfileStatus.ACTIVE
        assert "billing.open_on_publication.failed" in caplog.text


class TestSoloPublish:
    def test_pending_opens_solo_account_without_trial_then_moderation_starts_it(
        self, billing_on, olga, operator, django_capture_on_commit_callbacks,
    ):
        _make_ready(olga, OLGA, operator)

        with django_capture_on_commit_callbacks(execute=True):
            publish(olga, uuid.uuid4())

        sub = SpecialistSubscription.objects.get(tenant=olga.tenant)
        assert sub.tariff == TariffPlan.objects.get(code="solo")
        assert sub.status == SpecialistSubscription.Status.TRIAL
        assert sub.trial_ends_at is None, "PENDING не виден клиентам — пробный не идёт"
        assert _trial_events() == []

        _approve_on_commit(olga, operator, django_capture_on_commit_callbacks)

        sub.refresh_from_db()
        assert sub.trial_ends_at is not None
        assert sub.current_period_start == timezone.localdate()
        assert len(_trial_events()) == 1

    def test_replayed_publish_keeps_one_account(
        self, billing_on, olga, operator, django_capture_on_commit_callbacks,
    ):
        _make_ready(olga, OLGA, operator)
        command = uuid.uuid4()
        with django_capture_on_commit_callbacks(execute=True):
            publish(olga, command)
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            publish(olga, command)
            publish(olga, uuid.uuid4())  # already_pending — без перехода

        assert callbacks == [], "повтор и already_pending не зовут хук"
        assert SpecialistSubscription.objects.filter(tenant=olga.tenant).count() == 1


class TestNightlySweep:
    def test_opens_account_for_a_tenant_on_sale_without_one(self, billing_on):
        salon = _salon()
        master = _salon_master(salon)
        SpecialistProfile.objects.filter(pk=master.pk).update(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
        )
        hidden = _salon()
        _salon_master(hidden)  # PENDING — не в продаже

        assert start_pending_trials() == 1

        sub = SpecialistSubscription.objects.get(tenant=salon)
        assert sub.trial_ends_at is not None
        assert not SpecialistSubscription.objects.filter(tenant=hidden).exists()
        assert start_pending_trials() == 0

    def test_tenant_without_payer_is_skipped_not_fatal(self, billing_on, caplog):
        orphan = _salon(with_admin=False)
        ok = _salon()
        for tenant in (orphan, ok):
            SpecialistProfile.objects.filter(pk=_salon_master(tenant).pk).update(
                status=SpecialistProfile.ProfileStatus.ACTIVE,
            )

        with caplog.at_level(logging.ERROR, logger="billing.charges"):
            assert start_pending_trials() == 1

        assert SpecialistSubscription.objects.filter(tenant=ok).exists()
        assert "billing.trial_start.no_tenant_admin" in caplog.text


class TestHookIsIdempotentDirectly:
    def test_open_twice_one_row_one_event(self, billing_on):
        salon = _salon()
        SpecialistProfile.objects.filter(pk=_salon_master(salon).pk).update(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
        )

        open_on_publication(salon.pk)
        open_on_publication(salon.pk)

        assert SpecialistSubscription.objects.filter(tenant=salon).count() == 1
        assert len(_trial_events()) == 1
