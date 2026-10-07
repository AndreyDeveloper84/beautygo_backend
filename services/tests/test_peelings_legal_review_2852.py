"""DRF-2852 — четыре канона-пилинга салона пилота на юридической проверке.

Решение владельца 07.10. Миграция ``0047`` схему не меняет — это данные,
поэтому узлы заводят строки живыми моделями и зовут шаг напрямую (как
``test_master_select_migration_2406``): откат одного приложения оставил бы
колонки остальных.

Узлы держат:

* после шага каждый из четырёх канонов читается ``license_state_of`` как
  ``class_unconfirmed``, а до шага — как ``not_required``;
* провенанс полный: класс, кто, когда, основание;
* чужой канон — и пилинг с другим pk тоже — не тронут;
* класс, поставленный человеком, шаг не переписывает;
* без учётки владельца, с выключенной учёткой и с не-пилингом под pk шаг
  падает и ничего не пишет;
* миграция берёт автором именную учётку владельца, найденную по ключу;
* обратный ход снимает только своё;
* на базе без этих канонов шаг ничего не делает.
"""

from __future__ import annotations

import importlib
import uuid
from decimal import Decimal

import pytest
from django.apps import apps as live_apps
from django.contrib.auth.hashers import make_password
from django.utils import timezone

from services.body_care_license import CLASS_UNCONFIRMED, NOT_REQUIRED, license_states
from services.migrations import _drf2852_peelings_legal_review as peelings
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.migrations import _owner_provenance_account as owner_account
from users.models import User

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("services.migrations.0047_drf2852_peelings_legal_review")
LC = ServiceTemplate.LegalServiceClass

NAMES = ("Азелаиновый пилинг", "Миндальный пилинг", "Феруловый пилинг", "Чистка + пилинг")
CLASS_FIELDS = (
    "legal_service_class",
    "legal_class_confirmed_by_id",
    "legal_class_confirmed_at",
    "legal_class_source_ref",
)


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="drf2852-owner", password="x")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Косметология 2852", slug="cosm-2852")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="drf2852-salon", name="Салон 2852")


def _canon(category, name, pk=None) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        pk=pk or uuid.uuid4(), category=category, name=name, name_short=name[:40]
    )


@pytest.fixture
def canons(category):
    return [
        _canon(category, name, pk)
        for pk, name in zip(peelings.PEELING_TEMPLATE_IDS, NAMES, strict=True)
    ]


def _offer(salon, category, template) -> SalonService:
    return SalonService.objects.create(
        tenant=salon, category=category, template=template, name=template.name,
        duration_minutes=60, base_price=Decimal("3000"),
    )


def _close(by):
    return peelings.close(ServiceTemplate, User, confirmed_by_id=by.pk, now=timezone.now())


def _snapshot(templates):
    return list(
        ServiceTemplate.objects.filter(pk__in=[t.pk for t in templates])
        .order_by("pk")
        .values(*CLASS_FIELDS)
    )


# ─── что шаг закрывает ───────────────────────────────────────────────────────


def test_the_four_peelings_are_closed_for_the_license_gate(canons, category, salon, owner) -> None:
    offers = [_offer(salon, category, t) for t in canons]
    pks = [o.pk for o in offers]
    assert set(license_states(pks).values()) == {NOT_REQUIRED}

    counts = _close(owner)

    assert counts == {"found": 4, "closed": 4, "already_classed": 0}
    assert license_states(pks) == dict.fromkeys(pks, CLASS_UNCONFIRMED)


def test_the_provenance_is_complete(canons, owner) -> None:
    now = timezone.now()

    peelings.close(ServiceTemplate, User, confirmed_by_id=owner.pk, now=now)

    assert _snapshot(canons) == [
        {
            "legal_service_class": LC.LEGAL_REVIEW_REQUIRED,
            "legal_class_confirmed_by_id": owner.pk,
            "legal_class_confirmed_at": now,
            "legal_class_source_ref": peelings.SOURCE_REF,
        }
    ] * 4


def test_the_practitioner_class_is_left_unset(canons, owner) -> None:
    _close(owner)

    assert not ServiceTemplate.objects.filter(
        pk__in=peelings.PEELING_TEMPLATE_IDS, required_practitioner_class__isnull=False
    ).exists()


# ─── чего шаг не трогает ─────────────────────────────────────────────────────


def test_other_canons_are_untouched(canons, category, owner) -> None:
    others = [_canon(category, "Гликолевый пилинг"), _canon(category, "Массаж лица")]
    before = _snapshot(others)

    _close(owner)

    assert _snapshot(others) == before
    assert ServiceTemplate.objects.filter(legal_service_class__isnull=False).count() == 4


def test_a_class_set_by_a_human_is_not_rewritten(canons, owner) -> None:
    lawyer = User.objects.create_user(username="drf2852-lawyer", password="x")
    decided = canons[0]
    ServiceTemplate.objects.filter(pk=decided.pk).update(
        legal_service_class=LC.MEDICAL_COSMETOLOGY, legal_class_confirmed_by=lawyer,
        legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
    )
    before = _snapshot([decided])

    counts = _close(owner)

    assert counts == {"found": 4, "closed": 3, "already_classed": 1}
    assert _snapshot([decided]) == before


def test_a_database_without_these_canons_is_left_alone(category) -> None:
    _canon(category, "Гликолевый пилинг")

    counts = peelings.close(ServiceTemplate, User, confirmed_by_id=None, now=timezone.now())

    assert counts == {"found": 0, "closed": 0, "already_classed": 0}
    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()


# ─── когда шаг падает ────────────────────────────────────────────────────────


def test_no_named_author_refuses_and_writes_nothing(canons) -> None:
    with pytest.raises(peelings.CannotAttribute, match="учётки владельца нет"):
        peelings.close(ServiceTemplate, User, confirmed_by_id=None, now=timezone.now())

    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()


def test_an_author_absent_from_the_database_refuses(canons) -> None:
    with pytest.raises(peelings.CannotAttribute, match="в базе нет"):
        peelings.close(ServiceTemplate, User, confirmed_by_id=uuid.uuid4(), now=timezone.now())

    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()


def test_an_inactive_author_refuses(canons, owner) -> None:
    User.objects.filter(pk=owner.pk).update(is_active=False)

    with pytest.raises(peelings.CannotAttribute, match="в базе нет"):
        _close(owner)


def test_a_pk_that_is_not_a_peeling_refuses(category, owner) -> None:
    _canon(category, "Массаж лица", peelings.PEELING_TEMPLATE_IDS[0])
    _canon(category, "Миндальный пилинг", peelings.PEELING_TEMPLATE_IDS[1])

    with pytest.raises(peelings.CannotAttribute, match="не пилинг"):
        _close(owner)

    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()


# ─── обратный ход ────────────────────────────────────────────────────────────


def test_the_reverse_restores_the_state_before(canons, category, salon, owner) -> None:
    pks = [_offer(salon, category, t).pk for t in canons]
    before = _snapshot(canons)
    _close(owner)

    assert peelings.reopen(ServiceTemplate) == 4

    assert _snapshot(canons) == before
    assert set(license_states(pks).values()) == {NOT_REQUIRED}


def test_the_reverse_leaves_a_later_human_decision(canons, owner) -> None:
    _close(owner)
    decided = canons[0]
    # Класс и основание меняются порознь: отбор обратного хода держит оба.
    ServiceTemplate.objects.filter(pk=decided.pk).update(legal_service_class=LC.NON_MEDICAL_COSMETIC)
    reviewed = canons[1]
    ServiceTemplate.objects.filter(pk=reviewed.pk).update(legal_class_source_ref="юрист: оставить")
    before = _snapshot([decided, reviewed])

    assert peelings.reopen(ServiceTemplate) == 2

    assert _snapshot([decided, reviewed]) == before


def test_the_migration_refuses_when_there_is_no_owner_account(canons) -> None:
    User.objects.filter(username=owner_account.USERNAME).delete()

    with pytest.raises(peelings.CannotAttribute, match="учётки владельца нет"):
        MIGRATION.close_peelings(live_apps, None)

    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()


# ─── миграция и её литералы ──────────────────────────────────────────────────


@pytest.fixture
def owner_row(db):
    """Именная учётка владельца. Заводится здесь, а не берётся от накатки:
    транзакционный узел, отработавший раньше, смывает таблицы."""
    pk, _ = owner_account.ensure(User, unusable_password=make_password(None))
    return User.objects.get(pk=pk)


def test_the_migration_names_the_owner_account_as_the_author(canons, owner_row) -> None:
    author = owner_row

    MIGRATION.close_peelings(live_apps, None)
    assert ServiceTemplate.objects.filter(
        legal_service_class=LC.LEGAL_REVIEW_REQUIRED, legal_class_confirmed_by=author
    ).count() == 4

    MIGRATION.reopen_peelings(live_apps, None)
    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()


def test_the_migration_literals_match_the_model() -> None:
    assert peelings.LEGAL_REVIEW_REQUIRED == LC.LEGAL_REVIEW_REQUIRED.value
    assert 0 < len(peelings.SOURCE_REF) <= ServiceTemplate._meta.get_field(
        "legal_class_source_ref"
    ).max_length
    assert len(set(peelings.PEELING_TEMPLATE_IDS)) == 4
    for pk in peelings.PEELING_TEMPLATE_IDS:
        assert str(uuid.UUID(pk)) == pk


def test_the_migration_refuses_when_the_owner_account_is_switched_off(canons, owner_row) -> None:
    User.objects.filter(pk=owner_row.pk).update(is_active=False)

    with pytest.raises(peelings.CannotAttribute, match="учётки владельца нет"):
        MIGRATION.close_peelings(live_apps, None)

    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()
