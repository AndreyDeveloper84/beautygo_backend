"""Подтверждённый синоним канонической услуги (§93).

Пара, ради которой всё заведено, взята из живого замера 10.09::

    салон:  категория «Лазерная эпиляция» + услуга «Подмышки»
    канон:  «Лазерная эпиляция подмышек»                    (7.1.6)

Точным совпадением имени она не находится по устройству: салон держит
вид процедуры в категории, канон — в имени. Из 56 услуг пилота так
находились 8; ручная сверка эпиляции зона к зоне дала 15 из 17. То есть
нужда пилота — не «создать 48 канонов», а записать синонимы.

Файл проверяет ДВЕ вещи, и вторая важнее первой:

* синоним **находит** канон — иначе он бесполезен;
* синоним **ничего не решает** — иначе он опасен.

Второе — граница, за которой начинается выдумка. Позволить синониму
проставлять связь значило бы вернуть совпадение строк в доказательства
происхождения (§73), а §93 написан против этого. Синоним — находка,
связь — решение человека с его именем.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib import admin as django_admin
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils import timezone

from services.models import (
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    ServiceTemplateSynonym,
)
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

S = SalonService.MappingStatus


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="syn-tenant", name="Формула тела")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(
        name="Лазерная эпиляция", slug="syn-laser",
    )


@pytest.fixture
def human(db):
    return User.objects.create_user(username="syn-human", password="x")


@pytest.fixture
def canon(category):
    return ServiceTemplate.objects.create(
        category=category,
        name="Лазерная эпиляция подмышек",
        name_short="Эпиляция подмышек",
    )


def _synonym(canon, text="Подмышки", **overrides):
    fields = dict(
        template=canon, text=text,
        confirmed_at=timezone.now(),
        source_ref="разбор 56 услуг, строка 7",
    )
    fields.update(overrides)
    return ServiceTemplateSynonym.objects.create(**fields)


# ---------------------------------------------------------------------------
# Ключ поиска
# ---------------------------------------------------------------------------


def test_key_is_computed_on_save_not_typed(canon, human):
    """Нормализованный ключ считает модель, а не вызывающий.

    Строка приезжает миграцией, командой и админкой; ключ обязан
    получиться один и тот же во всех трёх случаях. Поэтому поле
    `editable=False` и заполняется в `save()`.
    """
    synonym = _synonym(canon, text="  Бёдра  ПОЛНОСТЬЮ ", confirmed_by=human)
    assert synonym.normalized == "бедра полностью"
    assert synonym.text == "  Бёдра  ПОЛНОСТЬЮ ", "исходная строка изменена"


def test_the_same_synonym_twice_is_a_duplicate(canon, human):
    """«Подмышки» и «подмышки » — одна запись, а не две.

    Уникальность по нормализованному ключу, а не по тексту: иначе обе
    висели бы в выдаче и оператор видел бы дубль, отличающийся
    пробелом.
    """
    _synonym(canon, text="Подмышки", confirmed_by=human)
    with pytest.raises(IntegrityError, match="template_normalized_uniq"):
        with transaction.atomic():
            _synonym(canon, text="подмышки ", confirmed_by=human)


def test_one_wording_may_lead_to_two_canons(category, human):
    """Один синоним вправе вести к нескольким канонам.

    Это **не ответ** на открытый вопрос владельцу («один канон или
    несколько»), а отказ отвечать за него кодом. Разрешать безопасно:
    синоним ничего не решает, оператор видит двух кандидатов и выбирает
    сам. Запретить было бы хуже — второй салон не смог бы записать своё
    настоящее название, не удалив чужое.
    """
    back = ServiceTemplate.objects.create(
        category=category, name="Массаж спины", name_short="Спина",
    )
    sport = ServiceTemplate.objects.create(
        category=category, name="Спортивный массаж спины", name_short="Спорт спина",
    )
    _synonym(back, text="Спина", confirmed_by=human)
    _synonym(sport, text="Спина", confirmed_by=human)

    assert ServiceTemplateSynonym.objects.filter(normalized="спина").count() == 2


# ---------------------------------------------------------------------------
# Провенанс: неподтверждённых синонимов эта таблица не хранит
# ---------------------------------------------------------------------------


def test_synonym_without_basis_is_refused(canon, human):
    """Без основания синоним не записывается.

    §93 говорит «подтверждённый синоним». Второго состояния нет, значит
    провенанс обязателен у каждой строки, а не у избранных.
    """
    with pytest.raises(IntegrityError, match="requires_provenance"):
        with transaction.atomic():
            _synonym(canon, confirmed_by=human, source_ref="")


def test_synonym_without_who_or_rule_is_refused(canon):
    """Без автора и без правила — тоже."""
    with pytest.raises(IntegrityError, match="requires_provenance"):
        with transaction.atomic():
            _synonym(canon)


def test_who_and_rule_together_are_refused(canon, human):
    """Кто ИЛИ правило, но не оба — как у связи."""
    with pytest.raises(IntegrityError, match="who_xor_rule"):
        with transaction.atomic():
            _synonym(
                canon, confirmed_by=human,
                confirmed_rule="category_prefix_match", rule_version="v1",
            )


def test_rule_without_version_is_refused(canon):
    """Правило без версии — «подтверждено какой-то из версий»."""
    with pytest.raises(IntegrityError, match="rule_carries_version"):
        with transaction.atomic():
            _synonym(canon, confirmed_rule="category_prefix_match")


def test_rule_with_version_is_accepted(canon):
    """Положительная стража: правило с версией проходит.

    Без неё три проверки выше зеленели бы и на схеме, которая
    запрещает синонимы вовсе.
    """
    synonym = _synonym(canon, confirmed_rule="category_prefix_match", rule_version="v1")
    assert synonym.confirmed_rule == "category_prefix_match"


# ---------------------------------------------------------------------------
# Ради чего всё: канон находится словами салона
# ---------------------------------------------------------------------------


def _template_search(term: str):
    """Поиск шаблона так, как его делает сама админка.

    Через `get_search_results`, а не через свой `filter`: всплывающее
    окно выбора шаблона на форме услуги салона ищет именно этим
    механизмом, и проверять надо его, иначе тест доказывает
    работоспособность собственного запроса.
    """
    model_admin = django_admin.site._registry[ServiceTemplate]
    request = RequestFactory().get("/admin/services/servicetemplate/")
    queryset, _ = model_admin.get_search_results(
        request, ServiceTemplate.objects.all(), term,
    )
    return list(queryset.distinct())


def test_canon_is_not_findable_by_the_salon_wording_without_a_synonym(canon):
    """Замер, ради которого срез и существует: без синонима не находится.

    Это положительная стража наоборот — она доказывает, что следующий
    тест проверяет работу синонима, а не то, что поиск и так работал.
    """
    assert _template_search("Подмышки") == []


def test_one_synonym_makes_the_canon_findable(canon, human):
    """С синонимом — находится, и именно тот канон.

    Это вся польза среза в одной строке: оператор, размечающий услугу
    «Подмышки», набирает своё слово во всплывающем окне и видит
    «Лазерная эпиляция подмышек».
    """
    _synonym(canon, text="Подмышки", confirmed_by=human)
    assert _template_search("Подмышки") == [canon]


def test_search_by_synonym_ignores_case_and_yo(canon, human):
    """Поиск не требует попасть в регистр и в букву ё."""
    _synonym(canon, text="Бёдра полностью", confirmed_by=human)
    assert _template_search("бёдра") == [canon]


# ---------------------------------------------------------------------------
# Граница: синоним находит, но ничего не решает
# ---------------------------------------------------------------------------


def test_synonym_does_not_link_the_salon_service(tenant, category, canon, human):
    """Появление синонима не связывает услугу салона с каноном.

    Совпадение строк не является доказательством происхождения (§73), и
    §93 написан ровно против того, чтобы оно им становилось. Синоним
    делает канон **находимым**; связывает человек.
    """
    service = SalonService.objects.create(
        tenant=tenant, category=category, name="Подмышки",
        duration_minutes=30, base_price=Decimal("900"),
    )
    _synonym(canon, text="Подмышки", confirmed_by=human)

    service.refresh_from_db()
    assert service.template_id is None, "синоним проставил связь"
    assert service.mapping_status == S.UNMAPPED


def test_synonym_does_not_make_anything_recommendable(tenant, category, canon, human):
    """И в подбор через синоним никто не попадает.

    Гейт §76 остаётся единственной дверью. Считается `== 0`, а не
    `<= before`: ноль обязан иметь положительную стражу, а здесь ноль
    и есть предмет.
    """
    SalonService.objects.create(
        tenant=tenant, category=category, name="Подмышки",
        duration_minutes=30, base_price=Decimal("900"),
    )
    _synonym(canon, text="Подмышки", confirmed_by=human)

    assert SalonService.objects.filter(mapping_status=S.VERIFIED).count() == 0


def test_synonym_carries_no_switch_that_could_apply_it(canon, human):
    """У синонима нет ни статуса, ни флага «применить».

    Проверка структурная и намеренно грубая: она краснеет, если кто-то
    заведёт на этой модели поле, превращающее находку в решение. Такое
    поле появится не по злому умыслу, а как «удобно», и заметить его в
    диффе на сорок строк легко не получится.
    """
    field_names = {f.name for f in ServiceTemplateSynonym._meta.get_fields()}
    forbidden = {"mapping_status", "status", "is_applied", "apply", "auto_link"}
    assert not (field_names & forbidden), (
        "на синониме появилось поле, превращающее находку в решение: "
        + ", ".join(sorted(field_names & forbidden))
    )
