"""Подтверждение связи оставляет след синонимом — §93 шаг 4.

Решение владельца перечисляет четыре шага: завести канон, связать,
одобрить, **сохранить исходное название салона подтверждённым
синонимом**. Четвёртый до сих пор не выполнял никто: таблица
`ServiceTemplateSynonym` приехала со своей админкой и **без единого
писателя** — ровно как `mapping_status` жил без писателя от §76 до §93.

Что здесь проверяется и в каком порядке:

* след появляется, когда решение принято;
* след **не** появляется во всех трёх случаях, когда его быть не
  должно, и у каждого своя причина;
* существующий синоним не переписывается;
* граница §93 цела — синоним по-прежнему ничего не решает.

Последнее важнее всего. Писатель делает синоним **следствием**
человеческого решения. Если бы из него получилось основание для связи,
мы вернули бы совпадение строк в доказательства происхождения — то, от
чего §93 и уводит.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib import admin as django_admin
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
    return Tenant.objects.create(slug="trace-tenant", name="Формула тела")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Лазерная эпиляция", slug="trace-laser")


@pytest.fixture
def human(db):
    return User.objects.create_user(username="trace-human", password="x")


@pytest.fixture
def canon(category):
    return ServiceTemplate.objects.create(
        category=category,
        name="Лазерная эпиляция подмышек",
        name_short="Эпиляция подмышек",
    )


def _service(tenant, category, name="Подмышки", **overrides):
    fields = dict(
        tenant=tenant, category=category, name=name,
        duration_minutes=30, base_price=Decimal("900"),
    )
    fields.update(overrides)
    return SalonService(**fields)


def _confirmed(tenant, category, canon, human, name="Подмышки", **overrides):
    return _service(
        tenant, category, name=name, template=canon,
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор 56 услуг, строка 7",
        **overrides,
    )


def _save_through_admin(service, actor=None):
    """Сохранить так, как это делает оператор — через `save_model`.

    Не `service.save()`: писатель живёт в админке намеренно (§93 про
    операторский поток), и проверять надо путь оператора. Прямое
    сохранение синонима не оставит, и отдельный тест ниже это
    закрепляет как назначенное свойство, а не как случайность.
    """
    model_admin = django_admin.site._registry[SalonService]
    request = RequestFactory().post("/admin/services/salonservice/add/")
    request.user = actor or User.objects.create_superuser(
        username=f"trace-op-{uuid.uuid4().hex[:8]}",
        password="pw",  # pragma: allowlist secret
        email="op@example.com",
        role="admin",
    )
    model_admin.save_model(request, service, form=None, change=False)
    return service


# ---------------------------------------------------------------------------
# След появляется
# ---------------------------------------------------------------------------


def test_confirming_a_link_records_the_salon_wording(tenant, category, canon, human):
    """Подтвердили связь — название салона стало синонимом канона.

    Это и есть шаг 4. Следующий салон, назвавший услугу так же,
    найдёт канон поиском, а не ручным перебором тридцати двух позиций
    эпиляции.
    """
    before = ServiceTemplateSynonym.objects.count()
    _save_through_admin(_confirmed(tenant, category, canon, human))

    assert ServiceTemplateSynonym.objects.count() == before + 1
    synonym = ServiceTemplateSynonym.objects.get()
    assert synonym.text == "Подмышки"
    assert synonym.template_id == canon.pk
    assert synonym.source_tenant_id == tenant.pk


def test_the_trace_carries_the_provenance_of_the_decision(tenant, category, canon, human):
    """Провенанс копируется со связи, а не сочиняется заново.

    Оператор принял ОДНО решение, и у синонима с связью обязан быть
    один автор, одна дата и одно основание. Подставить сюда «того, кто
    нажал сохранить» значило бы завести второго автора у одного
    решения.
    """
    service = _confirmed(tenant, category, canon, human)
    _save_through_admin(service)

    synonym = ServiceTemplateSynonym.objects.get()
    assert synonym.confirmed_by_id == human.pk
    assert synonym.source_ref == service.mapping_source_ref
    assert synonym.confirmed_at == service.mapping_confirmed_at


# ---------------------------------------------------------------------------
# След не появляется — три случая, у каждого своя причина
# ---------------------------------------------------------------------------


def test_no_trace_while_the_link_is_unconfirmed(tenant, category, canon):
    """Связь не подтверждена — утверждать нечего.

    `UNMAPPED` и `REVIEW_REQUIRED` ничего не говорят про то, чем
    является услуга. Синоним из них был бы утверждением, которого никто
    не делал.
    """
    _save_through_admin(_service(tenant, category, template=canon))
    assert not ServiceTemplateSynonym.objects.exists()


def test_no_trace_for_a_refusal(tenant, category, canon, human):
    """Отказ — не утверждение о каноне, а отказ от него.

    `NOT_RECOMMENDABLE` прямо говорит «связи не будет». Записать из
    него синоним значило бы утверждать ровно обратное решению
    оператора.
    """
    _save_through_admin(_service(
        tenant, category, template=canon,
        mapping_status=S.NOT_RECOMMENDABLE,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор 56 услуг, строка 41: это не процедура",
    ))
    assert not ServiceTemplateSynonym.objects.exists()


def test_no_trace_without_a_template(tenant, category, human):
    """Внетаксономическая услуга (D2) канона не имеет по устройству."""
    _save_through_admin(_service(
        tenant, category, name="Своя авторская программа",
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор 56 услуг, строка 55",
    ))
    assert not ServiceTemplateSynonym.objects.exists()


# ---------------------------------------------------------------------------
# Существующий синоним не переписывается
# ---------------------------------------------------------------------------


def test_existing_synonym_keeps_its_own_author(tenant, category, canon, human):
    """Чужой провенанс старше нашего и не затирается.

    Иначе автором синонима становился бы последний, кто сохранил
    услугу с таким же названием, — и запись отвечала бы на «кто
    сохранил», а не на «кто решил».
    """
    first = User.objects.create_user(username="trace-first", password="x")
    ServiceTemplateSynonym.objects.create(
        template=canon, text="подмышки",
        confirmed_by=first,
        confirmed_at=timezone.now(),
        source_ref="первый разбор",
    )

    _save_through_admin(_confirmed(tenant, category, canon, human))

    assert ServiceTemplateSynonym.objects.count() == 1, "завёлся дубль по ключу"
    assert ServiceTemplateSynonym.objects.get().confirmed_by_id == first.pk


def test_saving_twice_adds_one_synonym_not_two(tenant, category, canon, human):
    """Повторное сохранение той же услуги не плодит записи.

    Оператор правит строку не один раз: поменял цену, поправил
    длительность. Считается `== before + 1`, а не `>= before`: ноль
    обязан иметь положительную стражу, и «не выросло» здесь так же
    важно, как «выросло на единицу» выше.
    """
    service = _confirmed(tenant, category, canon, human)
    _save_through_admin(service)
    before = ServiceTemplateSynonym.objects.count()

    service.base_price = Decimal("1100")
    _save_through_admin(service)

    assert ServiceTemplateSynonym.objects.count() == before


# ---------------------------------------------------------------------------
# Граница §93 цела
# ---------------------------------------------------------------------------


def test_the_trace_does_not_link_anyone_else(tenant, category, canon, human):
    """Появившийся синоним не размечает соседнюю услугу.

    Синоним остался находкой. Если бы он начал проставлять связи,
    совпадение строк снова стало бы доказательством происхождения
    (§73) — то, против чего §93 и написан.
    """
    _save_through_admin(_confirmed(tenant, category, canon, human))

    neighbour = SalonService.objects.create(
        tenant=tenant, category=category, name="Подмышки",
        duration_minutes=30, base_price=Decimal("950"),
    )
    neighbour.refresh_from_db()

    assert neighbour.template_id is None
    assert neighbour.mapping_status == S.UNMAPPED


def test_direct_save_leaves_no_trace_and_that_is_by_design(tenant, category, canon, human):
    """Сохранение мимо админки синонима не оставляет — назначенно.

    Писатель живёт в операторском потоке, а не в `Model.save()`: иначе
    его звали бы миграции, сиды и любые скрипты, в том числе там, где
    «название салона» ничего не значит. Тест закрепляет цену этого
    решения, чтобы она была видна, а не обнаружилась однажды как
    пропажа.
    """
    _confirmed(tenant, category, canon, human).save()
    assert not ServiceTemplateSynonym.objects.exists()
