"""Происхождение ответа салона «нужна ли проверка перед услугой» (DRF-2877, S2).

Решение владельца 07.10: неподтверждённое «проверка не нужна» — «неизвестно»,
и смотреть надо происхождение **конкретного поля**, а не способ создания
строки услуги. До этой правки у ответа салона не было ни автора, ни даты.

Узлы держат:

* новая услуга и любое значение без подтверждения — «не подтверждён»; так
  же стоят все строки, заведённые до миграции;
* подтверждают ответ, а не молчание; у подтверждения есть автор — человек
  ИЛИ правило с версией, не оба и не ни одного, — дата и основание;
* неподтверждённый ответ не несёт ни автора, ни даты;
* смена ответа под прежним подтверждением снимает подтверждение — и через
  ``update_fields`` тоже; новое подтверждение в том же сохранении остаётся;
* форма админки говорит оператору по полю и до сохранения; форма, которая
  происхождение не прислала, не падает и ничего не подтверждает;
* вердикт гейта записи и его основание не меняются; единый вызов у ребра
  добавляет к ним один признак — подтверждено ли основание;
* сидер, копирующий флаг канона, ответом салона не становится;
* то же у флага канона: просмотр относится к значению флага, и переворот
  флага под прежним просмотром возвращает его в черновые;
* перепись читает новое поле без правки; указатель на подтвердившего решён
  переписью удаления.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib import admin as django_admin
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.deletion_executor import RETAIN
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

Origin = SalonService.HealthCheckAnswerOrigin
CONFIRMED_AT = timezone.now().replace(microsecond=123456) - timedelta(days=2)


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Происхождение 2877", slug="hc-origin-2877")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="hc-origin-salon", name="Салон происхождения")


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="hc-origin-curator", password="x", is_staff=True, is_superuser=True)


def _offer(salon, category, **over) -> SalonService:
    fields = {
        "tenant": salon, "category": category, "name": "Услуга салона",
        "duration_minutes": 60, "base_price": Decimal("3000"),
    }
    fields.update(over)
    return SalonService.objects.create(**fields)


def _confirmed(salon, category, curator, answer=False, **over) -> SalonService:
    return _offer(
        salon, category, requires_health_check=answer, health_check_origin=Origin.CONFIRMED,
        health_check_confirmed_by=curator, health_check_confirmed_at=CONFIRMED_AT,
        health_check_source_ref="ответ администратора салона 05.10", **over,
    )


def _stored(offer) -> dict:
    return SalonService.objects.filter(pk=offer.pk).values(
        "requires_health_check", "health_check_origin", "health_check_confirmed_by_id",
        "health_check_confirmed_rule", "health_check_rule_version", "health_check_confirmed_at",
        "health_check_source_ref",
    ).get()


def _refused(offer, constraint, **fields) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        with transaction.atomic():
            SalonService.objects.filter(pk=offer.pk).update(**fields)


# ─── умолчание ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("answer", [None, True, False])
def test_an_answer_nobody_confirmed_is_unset(salon, category, answer) -> None:
    """Значение без автора — не ответ человека, каким бы оно ни было."""
    offer = _offer(salon, category, requires_health_check=answer)

    stored = _stored(offer)
    assert stored["health_check_origin"] == Origin.UNSET
    assert not offer.health_check_answer_confirmed
    assert (stored["health_check_confirmed_by_id"], stored["health_check_confirmed_at"]) == (None, None)


def test_the_column_default_declares_nothing_for_rows_that_already_exist() -> None:
    """Миграция не подставляет подтверждений: умолчание колонки — «не подтверждён»."""
    assert SalonService._meta.get_field("health_check_origin").get_default() == Origin.UNSET
    assert set(Origin.values) == {"unset", "confirmed"}


# ─── база ────────────────────────────────────────────────────────────────────


def test_a_confirmed_answer_by_a_person_is_accepted(salon, category, curator) -> None:
    offer = _confirmed(salon, category, curator)

    assert offer.health_check_answer_confirmed
    assert _stored(offer)["health_check_confirmed_by_id"] == curator.pk


def test_a_confirmed_answer_by_a_named_rule_is_accepted(salon, category) -> None:
    offer = _offer(
        salon, category, requires_health_check=True, health_check_origin=Origin.CONFIRMED,
        health_check_confirmed_rule="owner_rule", health_check_rule_version="1",
        health_check_confirmed_at=CONFIRMED_AT, health_check_source_ref="решение владельца",
    )

    assert offer.health_check_answer_confirmed


def test_an_origin_outside_the_vocabulary_is_refused(salon, category) -> None:
    offer = _offer(salon, category)

    _refused(offer, "salonservice_health_check_origin_known", health_check_origin="seeded")


@pytest.mark.parametrize(
    "flaw",
    [
        {"requires_health_check": None},
        {"health_check_confirmed_at": None},
        {"health_check_source_ref": ""},
        {"health_check_confirmed_by": None},
        {"health_check_confirmed_rule": "owner_rule", "health_check_rule_version": "1"},
        {"health_check_confirmed_by": None, "health_check_confirmed_rule": "owner_rule"},
    ],
    ids=["молчание", "без-даты", "без-основания", "без-автора", "и-человек-и-правило", "правило-без-версии"],
)
def test_a_confirmed_answer_without_honest_provenance_is_refused(salon, category, curator, flaw) -> None:
    offer = _confirmed(salon, category, curator)

    _refused(offer, "salonservice_health_check_confirmed_requires_provenance", **flaw)


@pytest.mark.parametrize(
    "leftover",
    [
        {"health_check_confirmed_by": "curator"},
        {"health_check_confirmed_rule": "owner_rule", "health_check_rule_version": "1"},
        {"health_check_confirmed_at": CONFIRMED_AT},
    ],
    ids=["автор", "правило", "дата"],
)
def test_an_unset_answer_carries_no_confirmation(salon, category, curator, leftover) -> None:
    offer = _offer(salon, category, requires_health_check=False)
    fields = {k: (curator if v == "curator" else v) for k, v in leftover.items()}

    _refused(offer, "salonservice_health_check_unset_carries_no_confirmation", **fields)


# ─── подтверждение относится к ответу ────────────────────────────────────────


@pytest.mark.parametrize("new_answer", [True, None])
def test_a_new_answer_under_the_old_confirmation_drops_it(salon, category, curator, new_answer) -> None:
    offer = _confirmed(salon, category, curator, answer=False)

    offer.requires_health_check = new_answer
    offer.save()

    assert _stored(offer) == {
        "requires_health_check": new_answer,
        "health_check_origin": Origin.UNSET,
        "health_check_confirmed_by_id": None,
        "health_check_confirmed_rule": "",
        "health_check_rule_version": "",
        "health_check_confirmed_at": None,
        "health_check_source_ref": "",
    }


def test_update_fields_does_not_bypass_the_rule(salon, category, curator) -> None:
    offer = _confirmed(salon, category, curator, answer=False)

    offer.requires_health_check = True
    offer.save(update_fields=["requires_health_check"])

    stored = _stored(offer)
    assert (stored["requires_health_check"], stored["health_check_origin"]) == (True, Origin.UNSET)


def test_a_fresh_confirmation_in_the_same_save_is_kept(salon, category, curator) -> None:
    offer = _confirmed(salon, category, curator, answer=False)

    offer.requires_health_check = True
    offer.health_check_confirmed_at = CONFIRMED_AT + timedelta(days=2)
    offer.health_check_source_ref = "ответ администратора салона 07.10"
    offer.save()

    stored = _stored(offer)
    assert (stored["requires_health_check"], stored["health_check_origin"]) == (True, Origin.CONFIRMED)
    assert stored["health_check_source_ref"] == "ответ администратора салона 07.10"


def test_other_edits_leave_the_confirmation_alone(salon, category, curator) -> None:
    offer = _confirmed(salon, category, curator)
    before = _stored(offer)

    offer.name = "Услуга салона, новое имя"
    offer.base_price = Decimal("3500")
    offer.save()

    assert _stored(offer) == before


# ─── форма админки ───────────────────────────────────────────────────────────


def _form(curator, offer, **over):
    model_admin = django_admin.site._registry[SalonService]
    request = RequestFactory().get("/")
    request.user = curator
    at = timezone.localtime(CONFIRMED_AT)
    data = {
        "tenant": str(offer.tenant_id), "template": "", "category": str(offer.category_id),
        "name": offer.name, "duration_minutes": "60", "base_price": "3000", "is_active": True,
        "source": SalonService.Source.MANUAL, "mapping_status": SalonService.MappingStatus.UNMAPPED,
        "mapping_confirmed_by": "", "mapping_confirmed_rule": "", "mapping_rule_version": "",
        "mapping_source_ref": "",
        "requires_health_check": "false",
        "health_check_origin": Origin.CONFIRMED, "health_check_confirmed_by": str(curator.pk),
        "health_check_confirmed_rule": "", "health_check_rule_version": "",
        "health_check_confirmed_at_0": at.strftime("%Y-%m-%d"),
        "health_check_confirmed_at_1": at.strftime("%H:%M:%S"),
        "health_check_source_ref": "ответ администратора салона 05.10",
    }
    data.update(over)
    return model_admin.get_form(request, offer)(data=data, instance=offer)


def model_admin_form(curator, offer=None):
    request = RequestFactory().get("/")
    request.user = curator
    return django_admin.site._registry[SalonService].get_form(request, offer)


def _codes(form, field) -> list[str]:
    return [e.code for e in form.errors.as_data().get(field, [])]


def test_the_form_confirms_an_answer_with_its_author_date_and_ground(salon, category, curator) -> None:
    offer = _offer(salon, category, requires_health_check=False)

    form = _form(curator, offer)

    assert form.is_valid(), form.errors
    form.save()
    stored = _stored(offer)
    assert (stored["health_check_origin"], stored["health_check_confirmed_by_id"]) == (Origin.CONFIRMED, curator.pk)


@pytest.mark.parametrize(
    ("over", "field", "code"),
    [
        ({"requires_health_check": "unknown"}, "requires_health_check", "health_check_confirmed_requires_answer"),
        ({"health_check_confirmed_by": ""}, "health_check_confirmed_by",
         "health_check_confirmed_requires_who_or_rule"),
        ({"health_check_source_ref": ""}, "health_check_source_ref", "health_check_confirmed_requires_source_ref"),
        ({"health_check_confirmed_at_0": "", "health_check_confirmed_at_1": ""}, "health_check_confirmed_at",
         "health_check_confirmed_requires_at"),
        ({"health_check_confirmed_rule": "owner_rule", "health_check_rule_version": "1"},
         "health_check_confirmed_rule", "health_check_confirmed_who_xor_rule"),
        ({"health_check_origin": Origin.UNSET}, "health_check_origin", "health_check_unset_carries_confirmation"),
    ],
    ids=["молчание", "без-автора", "без-основания", "без-даты", "и-человек-и-правило", "не-подтверждён-но-с-автором"],
)
def test_the_form_names_the_field_instead_of_a_constraint(salon, category, curator, over, field, code) -> None:
    offer = _offer(salon, category, requires_health_check=False)

    form = _form(curator, offer, **over)

    assert not form.is_valid()
    assert code in _codes(form, field), form.errors


def test_a_form_that_does_not_send_the_origin_keeps_what_was_there(salon, category, curator) -> None:
    """Форма, которая о новом поле не знает, не падает и ничего не подтверждает."""
    silent = {
        "health_check_origin": "", "health_check_confirmed_by": "", "health_check_source_ref": "",
        "health_check_confirmed_at_0": "", "health_check_confirmed_at_1": "",
    }
    plain = _offer(salon, category, requires_health_check=False)

    form = _form(curator, plain, **silent)

    assert form.is_valid(), form.errors
    form.save()
    assert _stored(plain)["health_check_origin"] == Origin.UNSET

    new = model_admin_form(curator)(data={
        "tenant": str(salon.pk), "template": "", "category": str(category.pk), "name": "Новая услуга",
        "duration_minutes": "60", "base_price": "3000", "is_active": True,
        "source": SalonService.Source.MANUAL, "mapping_status": SalonService.MappingStatus.UNMAPPED,
        "requires_health_check": "false",
    })

    assert new.is_valid(), new.errors
    assert new.save().health_check_origin == Origin.UNSET


def test_the_form_tells_the_operator_the_answer_changed_under_its_confirmation(salon, category, curator) -> None:
    offer = _confirmed(salon, category, curator, answer=False)

    form = _form(curator, offer, requires_health_check="true")

    assert not form.is_valid()
    assert "health_check_answer_changed_needs_reconfirmation" in _codes(form, "requires_health_check")
    assert _stored(offer)["requires_health_check"] is False


def test_the_form_accepts_the_same_answer_with_its_truncated_date(salon, category, curator) -> None:
    """Положительная пара: без смены ответа та же форма проходит."""
    offer = _confirmed(salon, category, curator, answer=False)

    form = _form(curator, offer, name="Услуга салона, новое имя")

    assert form.is_valid(), form.errors
    form.save()
    assert _stored(offer)["health_check_origin"] == Origin.CONFIRMED


# ─── вердикт гейта не меняется ───────────────────────────────────────────────


@pytest.mark.parametrize("answer", [True, False])
def test_the_gate_verdict_and_its_basis_do_not_depend_on_the_origin(salon, category, curator, answer) -> None:
    """Здесь только различение: что открывает запись, решает владелец отдельно."""
    user = User.objects.create_user(username="hc-origin-master", password="x")
    master = SpecialistProfile.objects.create(
        user=user, tenant=salon, display_name="Мастер", status=SpecialistProfile.ProfileStatus.ACTIVE,
    )
    plain = _offer(salon, category, requires_health_check=answer, name="Без подтверждения")
    confirmed = _confirmed(salon, category, curator, answer=answer, name="С подтверждением")
    verdicts = [
        SpecialistService.objects.create(
            salon_service=offer, specialist=master, duration_minutes=60, price=Decimal("3000"),
        ).resolved_health_check()
        for offer in (plain, confirmed)
    ]

    assert verdicts == [(answer, "salon"), (answer, "salon")]


# ─── единый вызов: вердикт, основание, подтверждено ли ───────────────────────


def _edge(salon, offer, username, *, raised=False):
    user = User.objects.create_user(username=username, password="x")
    master = SpecialistProfile.objects.create(
        user=user, tenant=salon, display_name="Мастер", status=SpecialistProfile.ProfileStatus.ACTIVE,
    )
    return SpecialistService.objects.create(
        salon_service=offer, specialist=master, duration_minutes=60, price=Decimal("3000"),
        requires_health_check=raised,
    )


def _canon(category, curator, name, *, flag, confirmed) -> ServiceTemplate:
    canon = ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], requires_health_check=flag,
    )
    if confirmed:
        ServiceTemplate.objects.filter(pk=canon.pk).update(
            health_check_origin=ServiceTemplate.HealthCheckOrigin.CONFIRMED,
            health_check_confirmed_by=curator, health_check_confirmed_at=timezone.now(),
            health_check_source_ref="разбор владельца",
        )
    return canon


def test_one_call_says_whether_the_basis_of_the_verdict_is_confirmed(salon, category, curator) -> None:
    clear = _canon(category, curator, "Просмотрен, не нужна", flag=False, confirmed=True)
    gated = _canon(category, curator, "Просмотрен, нужна", flag=True, confirmed=True)
    draft_clear = _canon(category, curator, "Выведен, не нужна", flag=False, confirmed=False)
    draft_gated = _canon(category, curator, "Выведен, нужна", flag=True, confirmed=False)
    cases = {
        "канон просмотрен: нужна": (_offer(salon, category, template=gated, name="1"), False),
        "канон выведен: нужна": (_offer(salon, category, template=draft_gated, name="2"), False),
        "канон просмотрен: не нужна": (_offer(salon, category, template=clear, name="3"), False),
        "канон выведен: не нужна": (_offer(salon, category, template=draft_clear, name="4"), False),
        "салон без подтверждения: не нужна": (_offer(salon, category, requires_health_check=False, name="5"), False),
        "салон подтверждённо: не нужна": (_confirmed(salon, category, curator, answer=False, name="6"), False),
        "салон подтверждённо: нужна": (_confirmed(salon, category, curator, answer=True, name="7"), False),
        "мастер поднял": (_offer(salon, category, template=clear, name="8"), True),
        "никто не отвечал": (_offer(salon, category, name="9"), False),
        # Салон пол канона не опускает — даже подтверждённым «не нужна».
        "канон выведен нужна, салон подтверждённо не нужна": (
            _confirmed(salon, category, curator, answer=False, template=draft_gated, name="10"), False,
        ),
        # Подтверждённый ответ салона открывает и без просмотра флага канона.
        "канон выведен не нужна, салон подтверждённо не нужна": (
            _confirmed(salon, category, curator, answer=False, template=draft_clear, name="11"), False,
        ),
        # Поднятое без подтверждения побеждает подтверждённое «не нужна» канона.
        "канон просмотрен не нужна, салон без подтверждения нужна": (
            _offer(salon, category, template=clear, requires_health_check=True, name="12"), False,
        ),
    }

    answers = {}
    for n, (label, (offer, raised)) in enumerate(cases.items()):
        # Ребро перечитывается из базы: объекты в руках старше правки происхождения.
        edge = SpecialistService.objects.get(pk=_edge(salon, offer, f"hc-origin-master-{n}", raised=raised).pk)
        answer = edge.resolved_health_check_with_origin()
        # Каскад не повторяется: вердикт и основание — те же, что у гейта.
        assert answer[:2] == edge.resolved_health_check(), label
        answers[label] = answer

    assert answers == {
        "канон просмотрен: нужна": (True, "template_confirmed", True),
        "канон выведен: нужна": (True, "template_inferred", False),
        "канон просмотрен: не нужна": (False, "template_confirmed", True),
        "канон выведен: не нужна": (False, "template_inferred", False),
        "салон без подтверждения: не нужна": (False, "salon", False),
        "салон подтверждённо: не нужна": (False, "salon", True),
        "салон подтверждённо: нужна": (True, "salon", True),
        "мастер поднял": (True, "specialist", False),
        "никто не отвечал": (None, "unknown", False),
        "канон выведен нужна, салон подтверждённо не нужна": (True, "template_inferred", False),
        "канон выведен не нужна, салон подтверждённо не нужна": (False, "salon", True),
        "канон просмотрен не нужна, салон без подтверждения нужна": (True, "salon", False),
    }


# ─── то же у флага канона ────────────────────────────────────────────────────


def _canon_row(canon) -> dict:
    return ServiceTemplate.objects.filter(pk=canon.pk).values(
        "requires_health_check", "health_check_origin", "health_check_confirmed_by_id",
        "health_check_confirmed_at", "health_check_source_ref",
    ).get()


@pytest.mark.parametrize("flag", [True, False])
def test_a_canon_flag_flipped_under_its_review_goes_back_to_draft(category, curator, flag) -> None:
    """Просмотренное «нужна», перевёрнутое в «не нужна», открыло бы запись чужим просмотром."""
    canon = ServiceTemplate.objects.get(pk=_canon(category, curator, "Канон", flag=flag, confirmed=True).pk)

    canon.requires_health_check = not flag
    canon.save()

    assert _canon_row(canon) == {
        "requires_health_check": not flag,
        "health_check_origin": ServiceTemplate.HealthCheckOrigin.INFERRED,
        "health_check_confirmed_by_id": None,
        "health_check_confirmed_at": None,
        "health_check_source_ref": "",
    }


def test_a_canon_flag_flipped_with_a_fresh_review_stays_confirmed(category, curator) -> None:
    canon = ServiceTemplate.objects.get(pk=_canon(category, curator, "Канон", flag=True, confirmed=True).pk)

    canon.requires_health_check = False
    canon.health_check_confirmed_at = timezone.now() + timedelta(days=1)
    canon.health_check_source_ref = "повторный разбор владельца"
    canon.save()

    row = _canon_row(canon)
    assert (row["requires_health_check"], row["health_check_origin"]) == (
        False, ServiceTemplate.HealthCheckOrigin.CONFIRMED,
    )


def test_other_edits_of_a_canon_leave_its_review_alone(category, curator) -> None:
    canon = ServiceTemplate.objects.get(pk=_canon(category, curator, "Канон", flag=True, confirmed=True).pk)
    before = _canon_row(canon)

    canon.name_short = "Канон, короче"
    canon.sort_order = 7
    canon.save()

    assert _canon_row(canon) == before


def test_a_draft_canon_flag_may_be_flipped_freely(category, curator) -> None:
    canon = _canon(category, curator, "Черновой", flag=True, confirmed=False)

    canon.requires_health_check = False
    canon.save()

    row = _canon_row(canon)
    assert (row["requires_health_check"], row["health_check_origin"]) == (
        False, ServiceTemplate.HealthCheckOrigin.INFERRED,
    )


# ─── сидер, перепись, удаление ───────────────────────────────────────────────


def test_the_demo_seeder_copies_the_canon_flag_without_claiming_an_answer() -> None:
    """Сидер кладёт значение и не называет его подтверждённым ответом салона."""
    from services.management.commands import seed_demo_salons

    source = inspect.getsource(seed_demo_salons)

    assert '"requires_health_check": template.requires_health_check' in source
    assert "health_check_origin" not in source and "health_check_confirmed" not in source


def test_a_canon_keeps_its_own_origin_vocabulary() -> None:
    """Происхождение флага канона и ответа салона — разные словари, не одно поле."""
    assert set(ServiceTemplate.HealthCheckOrigin.values) == {"inferred", "confirmed"}
    assert Origin.CONFIRMED.value == ServiceTemplate.HealthCheckOrigin.CONFIRMED.value


def test_the_confirmer_is_decided_in_the_deletion_census() -> None:
    assert "services.SalonService.health_check_confirmed_by" in RETAIN
