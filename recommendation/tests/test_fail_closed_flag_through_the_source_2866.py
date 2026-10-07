"""Флаг ``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED`` — сквозь источник рекомендаций (DRF-2866).

Каталог под флагом начинает отдавать ``unclassified`` и перестаёт считать
неизвестный класс разрешением (``services.body_care_scope``). Здесь заперто,
что источник читает это так, как договорено с резолвером (#672): шов
``config_readiness`` и шов ``legal_gates`` на настоящих строках, при обоих
положениях флага.

Флаг включается переменной окружения на стенде, и узлы от этого не
краснеют: поведение под флагом держат только узлы, которые включают его
сами. Поэтому они нужны до включения, а не после.

Узлы держат:

* литерал каталога и литерал источника — один и тот же;
* под флагом неизвестная область, ``body_care`` без семейства и предложение
  без канона закрыты как ``UNCLASSIFIED``, а ``NOT_SUBJECT`` рождается
  только из подтверждённого ``not_body_care``;
* под флагом неподтверждённый класс закрывает юридический гейт у любого
  канона и у предложения без канона; открывает только подтверждённый
  немедицинский;
* две причины не сливаются: «область неизвестна» и «класс не подтверждён»
  приходят из разных гейтов и у одной строки могут стоять обе;
* при выключенном флаге оба шва отвечают по-старому — обход открыт.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from recommendation._types import ConfigGate, LegalGate
from services import body_care_validation
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users import recommendation_source
from users.models import SpecialistProfile, User
from users.recommendation_source import config_readiness, legal_gates

pytestmark = pytest.mark.django_db

Scope = ServiceTemplate.BodyCareScope
LC = ServiceTemplate.LegalServiceClass


@pytest.fixture
def closed(settings):
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="flag2866-staff", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="flag2866-salon", name="Салон флага")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Флаг 2866", slug="flag-2866")


@pytest.fixture
def master(db):
    user = User.objects.create_user(username="flag2866-master", password="x")
    return SpecialistProfile.objects.create(user=user, display_name="Мастер флага")


def _offer(salon, category, staff, name, *, scope=None, legal_class=None, canon=True) -> SalonService:
    template = None
    if canon:
        template = ServiceTemplate.objects.create(category=category, name=name, name_short=name[:40])
        fields = {}
        if scope is not None:
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


@pytest.fixture
def pool(salon, category, staff):
    return {
        "unknown": _offer(salon, category, staff, "Неизвестная"),
        "outside": _offer(salon, category, staff, "Лазер", scope=Scope.NOT_BODY_CARE),
        "review": _offer(
            salon, category, staff, "Пилинг", scope=Scope.NOT_BODY_CARE, legal_class=LC.LEGAL_REVIEW_REQUIRED
        ),
        "cleared": _offer(
            salon, category, staff, "Массаж", scope=Scope.NOT_BODY_CARE, legal_class=LC.NON_MEDICAL_COSMETIC
        ),
        "no_family": _offer(salon, category, staff, "Лимфодренаж", scope=Scope.BODY_CARE),
        "no_canon": _offer(salon, category, staff, "Без канона", canon=False),
    }


def _config(pool) -> dict[str, ConfigGate]:
    answers = config_readiness([o.pk for o in pool.values()])
    return {name: answers[offer.pk] for name, offer in pool.items()}


def _legal(pool, master) -> dict[str, LegalGate]:
    answers = legal_gates([(master.pk, o.pk, o.template_id) for o in pool.values()])
    return {name: answers[(master.pk, offer.pk)] for name, offer in pool.items()}


def test_the_catalog_marker_is_the_literal_the_source_knows() -> None:
    """Константа у каждого своя; расхождение должно краснеть."""
    assert body_care_validation.UNCLASSIFIED == recommendation_source.UNCLASSIFIED


def test_under_the_flag_the_unclassified_close_and_only_the_confirmed_skip(closed, pool) -> None:
    assert _config(pool) == {
        "unknown": ConfigGate.UNCLASSIFIED,
        "outside": ConfigGate.NOT_SUBJECT,
        "review": ConfigGate.NOT_SUBJECT,
        "cleared": ConfigGate.NOT_SUBJECT,
        "no_family": ConfigGate.UNCLASSIFIED,
        "no_canon": ConfigGate.UNCLASSIFIED,
    }


def test_under_the_flag_an_unconfirmed_class_closes_whatever_the_scope(closed, pool, master) -> None:
    assert _legal(pool, master) == {
        "unknown": LegalGate.CLASS_UNCONFIRMED,
        "outside": LegalGate.CLASS_UNCONFIRMED,
        "review": LegalGate.CLASS_UNCONFIRMED,
        "cleared": LegalGate.CLEARED,
        "no_family": LegalGate.CLASS_UNCONFIRMED,
        "no_canon": LegalGate.CLASS_UNCONFIRMED,
    }


def test_under_the_flag_only_a_classified_and_cleared_offer_is_open(closed, pool, master) -> None:
    config, legal = _config(pool), _legal(pool, master)
    opening = (ConfigGate.NOT_SUBJECT, ConfigGate.READY)

    assert {name for name in pool if config[name] in opening and legal[name] is LegalGate.CLEARED} == {
        "cleared"
    }
    # Причины не сливаются: у одной строки закрыты оба гейта, у другой — один.
    assert (config["unknown"], legal["unknown"]) == (ConfigGate.UNCLASSIFIED, LegalGate.CLASS_UNCONFIRMED)
    assert (config["outside"], legal["outside"]) == (ConfigGate.NOT_SUBJECT, LegalGate.CLASS_UNCONFIRMED)


def test_with_the_flag_off_both_seams_keep_the_old_answers(pool, master) -> None:
    config, legal = _config(pool), _legal(pool, master)

    assert set(config.values()) == {ConfigGate.NOT_SUBJECT}
    assert {name: legal[name] for name in ("unknown", "outside", "cleared", "no_family")} == dict.fromkeys(
        ("unknown", "outside", "cleared", "no_family"), LegalGate.CLEARED
    )
    assert legal["review"] is LegalGate.CLASS_UNCONFIRMED
