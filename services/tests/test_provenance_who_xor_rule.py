"""Кто ИЛИ правило, но не оба. Отдельный файл — намеренно.

Этот инвариант приезжает той же миграцией, что и четвёртое состояние
связи, но причина у него другая, и смешивать их тесты нельзя: при
откате одного второй потеряется молча.

**Что чинится.** Докстринг `SalonService.mapping_confirmed_by` со дня
своего появления говорит::

    Кто подтвердил. Взаимоисключающе с `mapping_confirmed_rule`:
    владелец назвал «кто ИЛИ какое правило», и оба сразу означали бы,
    что происхождение неизвестно точно.

А оба ограничения происхождения написаны через `OR` и строку с обоими
заполненными **пропускали**. То есть решение владельца §76 было
записано, не исполнялось и при этом выглядело исполненным — ровно то,
про что правило «проверяемый инвариант не живёт в комментарии».

Проверено подменой: на нетронутой модели первый тест зелёный, то есть
база такую строку принимает.

**Почему условие глобальное, а не только для терминальных состояний.**
Происхождение, набранное на ещё не решённой строке, тоже обязано быть
однозначным. Иначе неоднозначность просто дожидается смены статуса и
всплывает в тот момент, когда её меньше всего ждут.

Замер перед ужесточением (главное окно, 10.09.2026, `dev-web-1`): из
265 строк ноль имеют оба поля заполненными, ноль — только `by`, ноль —
только `rule`. Бэкфилла нет.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from services.models import SalonService, ServiceCategory
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

S = SalonService.MappingStatus


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="xor-tenant", name="Салон XOR")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Педикюр", slug="xor-pedicure")


@pytest.fixture
def human(db):
    return User.objects.create_user(username="xor-human", password="x")


def _service(tenant, category, name="Услуга", **overrides):
    fields = dict(
        tenant=tenant, category=category, name=name,
        duration_minutes=60, base_price=Decimal("1500"),
    )
    fields.update(overrides)
    return SalonService.objects.create(**fields)


def test_who_and_rule_together_are_refused_by_the_database(tenant, category, human):
    """Оба заполненных — не «двойная уверенность», а неизвестность.

    Проверяется базой, а не `clean()`: `clean()` обходится любым
    `update()` и любой миграцией данных, а именно так неоднозначное
    происхождение и появилось бы — не через форму.
    """
    with pytest.raises(IntegrityError, match="who_xor_rule"):
        with transaction.atomic():
            _service(
                tenant, category, name="И кто, и правило",
                mapping_status=S.VERIFIED,
                mapping_confirmed_by=human,
                mapping_confirmed_rule="exact_name_match",
                mapping_rule_version="v1",
                mapping_confirmed_at="2026-09-10T12:00:00Z",
                mapping_source_ref="DRF-1533",
            )


def test_who_and_rule_together_are_refused_even_when_undecided(tenant, category, human):
    """И на строке, про которую решения ещё нет.

    Условие глобальное. Если бы оно касалось только терминальных
    состояний, неоднозначное происхождение спокойно лежало бы на
    `unmapped`-строке и становилось нарушением ровно в тот момент, когда
    человек меняет статус, — то есть падало бы не там, где создано.
    """
    with pytest.raises(IntegrityError, match="who_xor_rule"):
        with transaction.atomic():
            _service(
                tenant, category, name="Неразобранная, но с двумя источниками",
                mapping_status=S.UNMAPPED,
                mapping_confirmed_by=human,
                mapping_confirmed_rule="exact_name_match",
                mapping_rule_version="v1",
            )


# ---------------------------------------------------------------------------
# Положительная стража: ужесточение не должно запретить законное
# ---------------------------------------------------------------------------


def test_who_alone_is_accepted(tenant, category, human):
    """Подтверждение человеком по-прежнему проходит."""
    service = _service(
        tenant, category, name="Только человек",
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at="2026-09-10T12:00:00Z",
        mapping_source_ref="разбор 56 услуг, строка 12",
    )
    assert service.mapping_confirmed_by == human


def test_rule_alone_is_accepted(tenant, category):
    """Подтверждение правилом с версией — тоже."""
    service = _service(
        tenant, category, name="Только правило",
        mapping_status=S.VERIFIED,
        mapping_confirmed_rule="exact_name_match",
        mapping_rule_version="v1",
        mapping_confirmed_at="2026-09-10T12:00:00Z",
        mapping_source_ref="DRF-1533",
    )
    assert service.mapping_confirmed_rule == "exact_name_match"


def test_neither_is_accepted_on_an_undecided_row(tenant, category):
    """Обычная неразобранная услуга заводится как заводилась.

    Ужесточение не должно превратиться в «происхождение обязательно
    всегда»: неразобранных строк на пилоте пятьдесят девять.
    """
    assert _service(tenant, category, name="Обычная").mapping_status == S.UNMAPPED
