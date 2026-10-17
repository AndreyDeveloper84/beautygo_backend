"""DRF-2883 — подтверждение связи не переживает смену канона.

Связь услуги салона с каноном подтверждают для конкретного канона. До этой
правки можно было сменить канон и оставить статус ``verified``, автора и
дату: услуга рекомендовалась как канон Б по подтверждению, выданному для
канона А. На пилоте таких строк не нашлось (замер главного окна 07.10: 59
подтверждённых, сработавших 0) — дефект латентный.

Узлы держат:

* смена канона у подтверждённой услуги без нового подтверждения возвращает
  связь в очередь проверки и снимает автора, правило и дату;
* новое подтверждение в том же сохранении — другая дата, другой автор или
  другое правило — принимается как есть;
* ``update_fields`` правило не обходит;
* прочие правки услуги подтверждение не трогают; неподтверждённую связь
  смена канона не трогает; новая услуга создаётся подтверждённой;
* форма админки говорит оператору до сохранения; дата, которую форма
  возвращает без микросекунд, за новое подтверждение не сходит;
* сидер стенда меняет канон вместе с новым подтверждением.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib import admin as django_admin
from django.test import RequestFactory
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

MS = SalonService.MappingStatus
CONFIRMED_AT = timezone.now().replace(microsecond=123456) - timedelta(days=3)


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Канон 2883", slug="canon-2883")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="canon-2883-salon", name="Салон 2883")


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="canon-2883-curator", password="x", is_staff=True, is_superuser=True)


@pytest.fixture
def canons(category):
    return [
        ServiceTemplate.objects.create(category=category, name=name, name_short=name)
        for name in ("Массаж спины", "Лазерная эпиляция")
    ]


def _offer(salon, category, canon, **over) -> SalonService:
    fields = {
        "tenant": salon, "category": category, "template": canon, "name": "Услуга салона",
        "duration_minutes": 60, "base_price": Decimal("3000"),
        "mapping_status": MS.VERIFIED, "mapping_confirmed_rule": "owner_rule",
        "mapping_rule_version": "1", "mapping_confirmed_at": CONFIRMED_AT,
        "mapping_source_ref": "разбор 01.10",
    }
    fields.update(over)
    return SalonService.objects.create(**fields)


def _stored(offer) -> dict:
    return SalonService.objects.filter(pk=offer.pk).values(
        "template_id", "mapping_status", "mapping_confirmed_by_id", "mapping_confirmed_rule",
        "mapping_rule_version", "mapping_confirmed_at", "mapping_source_ref",
    ).get()


# ─── модель ──────────────────────────────────────────────────────────────────


def test_a_new_canon_under_the_old_confirmation_sends_the_link_back_to_review(salon, category, canons) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage)

    offer.template = laser
    offer.save()

    assert _stored(offer) == {
        "template_id": laser.pk,
        "mapping_status": MS.REVIEW_REQUIRED,
        "mapping_confirmed_by_id": None,
        "mapping_confirmed_rule": "",
        "mapping_rule_version": "",
        "mapping_confirmed_at": None,
        # Основание остаётся следом, откуда связь взялась; подтверждением не служит.
        "mapping_source_ref": "разбор 01.10",
    }


def test_the_confirmation_of_a_person_is_dropped_the_same_way(salon, category, canons, curator) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage, mapping_confirmed_by=curator, mapping_confirmed_rule="",
                   mapping_rule_version="")

    offer.template = laser
    offer.save()

    stored = _stored(offer)
    assert (stored["mapping_status"], stored["mapping_confirmed_by_id"]) == (MS.REVIEW_REQUIRED, None)


def test_update_fields_does_not_bypass_the_rule(salon, category, canons) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage)

    offer.template = laser
    offer.save(update_fields=["template"])

    stored = _stored(offer)
    assert (stored["template_id"], stored["mapping_status"], stored["mapping_confirmed_at"]) == (
        laser.pk, MS.REVIEW_REQUIRED, None,
    )


@pytest.mark.parametrize(
    "fresh",
    [
        {"mapping_confirmed_at": CONFIRMED_AT + timedelta(days=3)},
        {"mapping_confirmed_rule": "another_rule"},
        {"mapping_rule_version": "2"},
    ],
    ids=["новая-дата", "другое-правило", "другая-версия-правила"],
)
def test_a_fresh_confirmation_in_the_same_save_is_kept(salon, category, canons, fresh) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage)

    offer.template = laser
    for name, value in fresh.items():
        setattr(offer, name, value)
    offer.save()

    stored = _stored(offer)
    assert (stored["template_id"], stored["mapping_status"]) == (laser.pk, MS.VERIFIED)
    assert stored["mapping_confirmed_at"] is not None


def test_a_new_confirming_person_in_the_same_save_is_kept(salon, category, canons, curator) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage)

    offer.template = laser
    offer.mapping_confirmed_by = curator
    offer.mapping_confirmed_rule = ""
    offer.mapping_rule_version = ""
    offer.save()

    stored = _stored(offer)
    assert (stored["mapping_status"], stored["mapping_confirmed_by_id"]) == (MS.VERIFIED, curator.pk)


def test_other_edits_leave_the_confirmation_alone(salon, category, canons) -> None:
    offer = _offer(salon, category, canons[0])
    before = _stored(offer)

    offer.name = "Услуга салона, новое имя"
    offer.base_price = Decimal("3500")
    offer.is_active = False
    offer.save()

    assert _stored(offer) == before


def test_an_unconfirmed_link_may_change_its_canon(salon, category, canons) -> None:
    massage, laser = canons
    offer = _offer(
        salon, category, massage, mapping_status=MS.REVIEW_REQUIRED, mapping_confirmed_rule="",
        mapping_rule_version="", mapping_confirmed_at=None, mapping_source_ref="master_select:1",
    )

    offer.template = laser
    offer.save()

    stored = _stored(offer)
    assert (stored["template_id"], stored["mapping_status"], stored["mapping_source_ref"]) == (
        laser.pk, MS.REVIEW_REQUIRED, "master_select:1",
    )


def test_a_new_offer_is_created_verified(salon, category, canons) -> None:
    offer = _offer(salon, category, canons[0])

    assert _stored(offer)["mapping_status"] == MS.VERIFIED


# ─── форма админки ───────────────────────────────────────────────────────────


def _form(curator, offer, **over):
    model_admin = django_admin.site._registry[SalonService]
    request = RequestFactory().get("/")
    request.user = curator
    data = {
        "tenant": str(offer.tenant_id), "template": str(offer.template_id),
        "category": str(offer.category_id), "name": offer.name, "duration_minutes": "60",
        "base_price": "3000", "is_active": True, "source": SalonService.Source.MANUAL,
        "mapping_status": MS.VERIFIED, "mapping_confirmed_by": "",
        "mapping_confirmed_rule": "owner_rule", "mapping_rule_version": "1",
        # Форма возвращает дату без микросекунд — как её видит оператор.
        "mapping_confirmed_at_0": timezone.localtime(CONFIRMED_AT).strftime("%Y-%m-%d"),
        "mapping_confirmed_at_1": timezone.localtime(CONFIRMED_AT).strftime("%H:%M:%S"),
        "mapping_source_ref": "разбор 01.10",
    }
    data.update(over)
    return model_admin.get_form(request, offer)(data=data, instance=offer)


def test_the_form_tells_the_operator_before_saving(salon, category, canons, curator) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage)

    form = _form(curator, offer, template=str(laser.pk))

    assert not form.is_valid()
    assert [e.code for e in form.errors.as_data()["template"]] == ["canon_changed_needs_reconfirmation"]
    assert _stored(offer)["template_id"] == massage.pk


def test_the_form_accepts_the_same_canon_with_its_truncated_date(salon, category, canons, curator) -> None:
    """Положительная пара: без смены канона та же форма проходит."""
    offer = _offer(salon, category, canons[0])

    form = _form(curator, offer, name="Услуга салона, новое имя")

    assert form.is_valid(), form.errors
    form.save()
    assert _stored(offer)["mapping_status"] == MS.VERIFIED


def test_the_form_accepts_a_new_canon_with_a_fresh_confirmation(salon, category, canons, curator) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage)
    today = timezone.localtime()

    form = _form(
        curator, offer, template=str(laser.pk), mapping_confirmed_by=str(curator.pk),
        mapping_confirmed_rule="", mapping_rule_version="",
        mapping_confirmed_at_0=today.strftime("%Y-%m-%d"), mapping_confirmed_at_1=today.strftime("%H:%M:%S"),
    )

    assert form.is_valid(), form.errors
    form.save()
    stored = _stored(offer)
    assert (stored["template_id"], stored["mapping_status"], stored["mapping_confirmed_by_id"]) == (
        laser.pk, MS.VERIFIED, curator.pk,
    )


def test_the_form_lets_the_operator_send_the_link_to_review_himself(salon, category, canons, curator) -> None:
    massage, laser = canons
    offer = _offer(salon, category, massage)

    form = _form(curator, offer, template=str(laser.pk), mapping_status=MS.REVIEW_REQUIRED)

    assert form.is_valid(), form.errors
    form.save()
    assert _stored(offer)["mapping_status"] == MS.REVIEW_REQUIRED


# ─── сидер стенда ────────────────────────────────────────────────────────────


def test_the_stand_seeder_reconfirms_when_it_changes_the_canon() -> None:
    """Сидер меняет канон у подтверждённой строки — и подтверждает заново."""
    import inspect

    from recommendation.management.commands import seed_golden

    source = inspect.getsource(seed_golden)
    change = source[source.index("salon.template = template"):]
    saved = change[: change.index("salon.save(") + 200]

    assert "salon.mapping_confirmed_at = timezone.now()" in saved
    assert '"mapping_confirmed_at"' in saved
