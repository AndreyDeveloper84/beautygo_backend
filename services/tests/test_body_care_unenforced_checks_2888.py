"""Какие проверки допуска сейчас не действуют из-за выключенного флага (DRF-2888).

Решение владельца 07.10: проверки рекомендаций сами по себе обход
классификации не закрывают — нужно показывать, какие из них реально работают
при текущих флагах. Общая функция допуска резолвера (``users.admission``)
различает «проверка пройдена» и «проверка не действует»; правило, по
которому проверка не действует, остаётся в каталоге, в одном месте.

Узлы держат:

* при включённом флаге недействующих проверок нет ни у какой строки;
* при выключенном не действует ровно то, чей ответ включение флага изменит:
  область — у неклассифицированного; класс, лицензия и адрес — у канона без
  семейства и без класса; квалификация — при неизвестном классе без
  заданного требования;
* строка, которую включение не затронет, в ответ не входит;
* ответ совпадает с тем, что на самом деле меняется у читателей каталога
  при переключении флага — на настоящих строках;
* набор литералов закрыт;
* флаг читается в момент вызова.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from services.body_care_address import address_states
from services.body_care_license import license_states
from services.body_care_qualification import qualification_states
from services.body_care_scope import UNENFORCEABLE_CHECKS, unenforced_checks
from services.body_care_validation import validation_states
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.models import SpecialistProfile, User

Scope = ServiceTemplate.BodyCareScope
Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass
PC = ServiceTemplate.PractitionerClass

LEGAL = {"legal_class", "license", "address"}


def _row(**over) -> dict:
    row = {"has_canon": True, "scope": None, "family": None, "legal_class": None}
    row.update(over)
    return row


# ─── правило, без базы ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        # Обход целиком: область неизвестна, класса нет.
        (_row(), {"scope", *LEGAL, "qualification"}),
        # Предложение без канона — то же.
        (_row(has_canon=False), {"scope", *LEGAL, "qualification"}),
        # Область подтверждена, класса нет: область от флага не зависит.
        (_row(scope=Scope.NOT_BODY_CARE), {*LEGAL, "qualification"}),
        # «Подлежит, семейство не определено» — область всё ещё не действует.
        (_row(scope=Scope.BODY_CARE), {"scope", *LEGAL, "qualification"}),
        # Канон с семейством и без класса: лицензия и адрес закрыты уже
        # сегодня (class_unconfirmed), не действует только квалификация.
        (_row(scope=Scope.BODY_CARE, family=Family.BODY_WRAP), {"qualification"}),
        # Класс на юридической проверке: лицензия и адрес закрыты уже сегодня.
        (_row(scope=Scope.NOT_BODY_CARE, legal_class=LC.LEGAL_REVIEW_REQUIRED), {"qualification"}),
        # Подтверждённый немедицинский класс и область: флаг ничего не меняет.
        (_row(scope=Scope.NOT_BODY_CARE, legal_class=LC.NON_MEDICAL_COSMETIC), set()),
        # Медицинский класс проверяется при любом флаге.
        (_row(scope=Scope.NOT_BODY_CARE, legal_class=LC.MEDICAL_COSMETOLOGY), set()),
        # Класс подтверждён, область нет.
        (_row(legal_class=LC.NON_MEDICAL_COSMETIC), {"scope"}),
    ],
)
def test_a_check_is_unenforced_exactly_where_the_flag_would_change_its_answer(row, expected) -> None:
    assert unenforced_checks(**row) == frozenset(expected)


def test_a_stated_qualification_requirement_is_enforced_whatever_the_flag() -> None:
    row = _row(scope=Scope.NOT_BODY_CARE)

    assert "qualification" in unenforced_checks(**row)
    assert "qualification" not in unenforced_checks(**row, required_practitioner_class=PC.NURSE_COSMETOLOGY)


def test_with_the_flag_on_nothing_is_unenforced(settings) -> None:
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True

    for row in (_row(), _row(has_canon=False), _row(scope=Scope.BODY_CARE)):
        assert unenforced_checks(**row) == frozenset()


def test_the_flag_is_read_at_the_moment_of_the_call(settings) -> None:
    assert unenforced_checks(**_row())

    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True
    assert not unenforced_checks(**_row())

    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = False
    assert unenforced_checks(**_row())


def test_the_set_of_literals_is_closed() -> None:
    assert UNENFORCEABLE_CHECKS == ("scope", "legal_class", "license", "address", "qualification")
    every = set()
    for has_canon in (True, False):
        for scope in (None, Scope.BODY_CARE, Scope.NOT_BODY_CARE):
            for family in (None, Family.BODY_WRAP):
                for legal_class in (None, *LC.values):
                    every |= unenforced_checks(
                        has_canon=has_canon, scope=scope, family=family, legal_class=legal_class
                    )
    assert every == set(UNENFORCEABLE_CHECKS)


# ─── то же самое — по настоящим читателям ────────────────────────────────────


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="unenforced-staff", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="unenforced-salon", name="Салон 2888")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Недействующие 2888", slug="unenforced-2888")


@pytest.fixture
def master(salon):
    user = User.objects.create_user(username="unenforced-master", password="x")
    return SpecialistProfile.objects.create(user=user, tenant=salon, display_name="Мастер")


def _offer(salon, category, staff, name, *, scope=None, family=None, legal_class=None, canon=True):
    template = None
    if canon:
        template = ServiceTemplate.objects.create(
            category=category, name=name, name_short=name[:40],
            service_family=family, canonical_version="1" if family else "",
        )
        fields = {}
        if scope is not None and not family:
            fields.update(
                body_care_scope=scope, scope_confirmed_by=staff,
                scope_confirmed_at=timezone.now(), scope_source_ref="решение владельца",
            )
        if legal_class is not None:
            fields.update(
                legal_service_class=legal_class, legal_class_confirmed_by=staff,
                legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
            )
        if fields:
            ServiceTemplate.objects.filter(pk=template.pk).update(**fields)
    return SalonService.objects.create(
        tenant=salon, category=category, template=template, name=name,
        duration_minutes=60, base_price=Decimal("3000"),
    )


@pytest.mark.django_db
def test_it_names_exactly_what_the_readers_answer_differently_under_the_flag(
    settings, salon, category, staff, master,
) -> None:
    """Сверка с первоисточником: что на самом деле меняется у читателей."""
    offers = [
        _offer(salon, category, staff, "Неизвестная"),
        _offer(salon, category, staff, "Лазер", scope=Scope.NOT_BODY_CARE),
        _offer(salon, category, staff, "Пилинг", scope=Scope.NOT_BODY_CARE, legal_class=LC.LEGAL_REVIEW_REQUIRED),
        _offer(salon, category, staff, "Массаж", scope=Scope.NOT_BODY_CARE, legal_class=LC.NON_MEDICAL_COSMETIC),
        _offer(salon, category, staff, "Лимфодренаж", scope=Scope.BODY_CARE),
        _offer(salon, category, staff, "Обёртывание", family=Family.BODY_WRAP),
        _offer(salon, category, staff, "Без канона", canon=False),
    ]
    pks = [o.pk for o in offers]
    pairs = [(master.pk, o.pk) for o in offers]
    canons = [(master.pk, o.template_id) for o in offers if o.template_id]

    def read():
        qualification = {k[1]: v.state for k, v in qualification_states(canons).items()}
        return (
            validation_states(pks), license_states(pks),
            {k[1]: v for k, v in address_states(pairs).items()}, qualification,
        )

    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = False
    off = read()
    expected = {}
    for offer in offers:
        # Канон читается из базы: объект в руках старше правки области и класса.
        canon = ServiceTemplate.objects.filter(pk=offer.template_id).first()
        expected[offer.pk] = unenforced_checks(
            has_canon=canon is not None,
            scope=canon.body_care_scope if canon else None,
            family=canon.service_family if canon else None,
            legal_class=canon.legal_service_class if canon else None,
        )
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True
    on = read()

    for offer in offers:
        changed = set()
        if off[0][offer.pk] != on[0][offer.pk]:
            changed.add("scope")
        if off[1][offer.pk] != on[1][offer.pk]:
            changed.update({"legal_class", "license"})
        if off[2][offer.pk] != on[2][offer.pk]:
            changed.add("address")
        if offer.template_id and off[3][offer.template_id] != on[3][offer.template_id]:
            changed.add("qualification")
        named = set(expected[offer.pk])
        if offer.template_id is None:
            # У предложения без канона квалификацию читатель не спрашивает вовсе.
            named.discard("qualification")
        assert named == changed, offer.name
