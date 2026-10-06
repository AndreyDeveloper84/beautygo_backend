"""Body Care §7A-0: юридический класс и требуемая квалификация на каноне (DRF-2801).

Контракт v0.2 §2.1 / §7A.1 / §7A.4. Решение главного окна (06.10, вариант 1,
по запрету владельца «класс без автора нельзя»): оба значения —
подтверждённые, пишет их только человек, с провенансом.

Узлы держат:

* словари — ровно четыре класса §7A.1 и пять квалификаций §7A.4;
* значение вне словаря база не примет;
* ``NULL`` по умолчанию: класс никому не присвоен;
* заданный класс без автора, даты или основания — отказ базы (и через
  ``update()``); то же для квалификации; провенансы независимы;
* оба указателя на подтвердившего решены переписью удаления.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.models import ServiceCategory, ServiceTemplate
from users.models import User

pytestmark = pytest.mark.django_db

LC = ServiceTemplate.LegalServiceClass
PC = ServiceTemplate.PractitionerClass


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care 7A-0", slug="bc-7a0")


@pytest.fixture
def lawyer(db):
    return User.objects.create_user(username="bc7a0-lawyer", password="x")


@pytest.fixture
def template(category):
    return ServiceTemplate.objects.create(
        category=category,
        name="Обёртывание 7A-0",
        name_short="Обёртывание",
        service_family=ServiceTemplate.ServiceFamily.BODY_WRAP,
        canonical_version="1",
    )


def _refused(template, constraint, **fields):
    with pytest.raises(IntegrityError, match=constraint):
        with transaction.atomic():
            ServiceTemplate.objects.filter(pk=template.pk).update(**fields)


# ─── словари ─────────────────────────────────────────────────────────────────


def test_the_legal_classes_are_exactly_the_four_of_7a1() -> None:
    assert set(LC.values) == {
        "non_medical_cosmetic", "medical_cosmetology", "medical_other", "legal_review_required",
    }


def test_the_practitioner_classes_are_exactly_the_five_of_7a4() -> None:
    assert set(PC.values) == {
        "cosmetic_esthetician", "nurse_cosmetology", "physician_cosmetologist",
        "medical_specialist", "protocol_specific",
    }


def test_a_new_canon_has_no_class_assigned(template) -> None:
    template.refresh_from_db()

    assert template.legal_service_class is None
    assert template.required_practitioner_class is None


def test_a_legal_class_outside_the_vocabulary_is_refused(template, lawyer) -> None:
    _refused(
        template,
        "servicetemplate_legal_service_class_known",
        legal_service_class="other",
        legal_class_confirmed_by=lawyer,
        legal_class_confirmed_at=timezone.now(),
        legal_class_source_ref="решение юриста",
    )


def test_a_practitioner_class_outside_the_vocabulary_is_refused(template, lawyer) -> None:
    _refused(
        template,
        "servicetemplate_practitioner_class_known",
        required_practitioner_class="any",
        practitioner_class_confirmed_by=lawyer,
        practitioner_class_confirmed_at=timezone.now(),
        practitioner_class_source_ref="решение клиники",
    )


# ─── класс без автора нельзя ─────────────────────────────────────────────────


def test_a_confirmed_legal_class_is_accepted(template, lawyer) -> None:
    ServiceTemplate.objects.filter(pk=template.pk).update(
        legal_service_class=LC.NON_MEDICAL_COSMETIC,
        legal_class_confirmed_by=lawyer,
        legal_class_confirmed_at=timezone.now(),
        legal_class_source_ref="решение владельца 06.10",
    )
    template.refresh_from_db()

    assert template.legal_service_class == LC.NON_MEDICAL_COSMETIC


@pytest.mark.parametrize("missing", ["by", "at", "source_ref"])
def test_a_legal_class_without_confirmation_is_refused(template, lawyer, missing) -> None:
    fields = {
        "legal_service_class": LC.NON_MEDICAL_COSMETIC,
        "legal_class_confirmed_by": lawyer,
        "legal_class_confirmed_at": timezone.now(),
        "legal_class_source_ref": "решение владельца",
    }
    blank = {"by": None, "at": None, "source_ref": ""}[missing]
    fields[{"by": "legal_class_confirmed_by", "at": "legal_class_confirmed_at",
            "source_ref": "legal_class_source_ref"}[missing]] = blank

    _refused(template, "servicetemplate_legal_class_requires_confirmation", **fields)


@pytest.mark.parametrize("missing", ["by", "at", "source_ref"])
def test_a_practitioner_class_without_confirmation_is_refused(template, lawyer, missing) -> None:
    fields = {
        "required_practitioner_class": PC.COSMETIC_ESTHETICIAN,
        "practitioner_class_confirmed_by": lawyer,
        "practitioner_class_confirmed_at": timezone.now(),
        "practitioner_class_source_ref": "решение клиники",
    }
    blank = {"by": None, "at": None, "source_ref": ""}[missing]
    fields[{"by": "practitioner_class_confirmed_by", "at": "practitioner_class_confirmed_at",
            "source_ref": "practitioner_class_source_ref"}[missing]] = blank

    _refused(template, "servicetemplate_practitioner_class_requires_confirmation", **fields)


def test_the_legal_confirmation_does_not_cover_the_practitioner_class(template, lawyer) -> None:
    """Решение юриста о классе не подтверждает решение клиники о квалификации."""
    _refused(
        template,
        "servicetemplate_practitioner_class_requires_confirmation",
        legal_service_class=LC.MEDICAL_COSMETOLOGY,
        legal_class_confirmed_by=lawyer,
        legal_class_confirmed_at=timezone.now(),
        legal_class_source_ref="решение юриста",
        required_practitioner_class=PC.PHYSICIAN_COSMETOLOGIST,
    )


# ─── перепись удаления ──────────────────────────────────────────────────────


def test_the_erasure_census_decides_both_confirmers() -> None:
    from users.deletion_executor import RETAIN

    assert "services.ServiceTemplate.legal_class_confirmed_by" in RETAIN
    assert "services.ServiceTemplate.practitioner_class_confirmed_by" in RETAIN
