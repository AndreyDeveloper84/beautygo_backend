"""Миграция ``0048`` на настоящих строках: область канонов с семейством.

Узлы рядом (``test_body_care_scope_fail_closed``) проверяют схему и правило
на уже накаченной базе. Здесь проверяется то, чего там не видно: что
ограничение «семейство требует области» **встаёт** на базе, где каноны с
семейством уже лежат, и что обратный ход проходит.

База откатывается на ``0047`` и накатывается обратно — поэтому узел идёт вне
транзакции. Строки заводятся историческими моделями состояния ``0047``:
живая модель про поле области уже знает, а колонки ещё нет.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("services", "0047_drf2852_peelings_legal_review")]
AFTER = [("services", "0048_body_care_scope")]


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    return MigrationExecutor(connection).loader.project_state(target).apps


@pytest.fixture
def at_0047(transactional_db):
    apps = _migrate(BEFORE)
    try:
        yield apps
    finally:
        # Вернуть базу на голову, даже если узел упал посередине: иначе
        # следующий узел сессии получит базу без колонок области.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_canons_with_a_family_get_the_scope_and_the_constraint_stands(at_0047) -> None:
    Category = at_0047.get_model("services", "ServiceCategory")
    Template = at_0047.get_model("services", "ServiceTemplate")
    category = Category.objects.create(name="Миграция 0048", slug="mig-0048")
    wraps = [
        Template.objects.create(
            category=category, name=f"Обёртывание {n}", name_short=f"Обёртывание {n}",
            service_family="body_wrap", canonical_version="1",
        )
        for n in range(2)
    ]
    plain = Template.objects.create(category=category, name="Массаж", name_short="Массаж")

    apps = _migrate(AFTER)

    Template = apps.get_model("services", "ServiceTemplate")
    scoped = list(
        Template.objects.filter(pk__in=[w.pk for w in wraps]).values_list(
            "body_care_scope", "scope_confirmed_by_id", "scope_confirmed_rule", "scope_rule_version",
        )
    )
    assert scoped == [("body_care", None, "family_implies_body_care", "1")] * 2
    assert not Template.objects.filter(
        pk__in=[w.pk for w in wraps], scope_confirmed_at__isnull=True
    ).exists()
    assert not Template.objects.filter(pk__in=[w.pk for w in wraps], scope_source_ref="").exists()
    # Канон без семейства шаг не трогает: его область неизвестна.
    assert Template.objects.get(pk=plain.pk).body_care_scope is None

    Template.objects.filter(pk__in=[*(w.pk for w in wraps), plain.pk]).delete()
    Category.objects.filter(pk=category.pk).delete()


def test_the_migration_reverses_with_scoped_canons_in_place(at_0047) -> None:
    Category = at_0047.get_model("services", "ServiceCategory")
    Template = at_0047.get_model("services", "ServiceTemplate")
    category = Category.objects.create(name="Миграция 0048 назад", slug="mig-0048-back")
    wrap = Template.objects.create(
        category=category, name="Обёртывание назад", name_short="Обёртывание",
        service_family="body_wrap", canonical_version="1",
    )
    _migrate(AFTER)

    apps = _migrate(BEFORE)

    Template = apps.get_model("services", "ServiceTemplate")
    assert Template.objects.get(pk=wrap.pk).service_family == "body_wrap"

    Template.objects.filter(pk=wrap.pk).delete()
    apps.get_model("services", "ServiceCategory").objects.filter(pk=category.pk).delete()
