"""Body Care CAT-2: у канона четыре состояния контракта на ОДНОЙ оси ``Lifecycle``.

Контракт Body Care v0.1 §2.1 требует статус DRAFT / CANDIDATE / ACTIVE /
RETIRED. Решение главного окна 06.10 — расширить существующий ``Lifecycle``
(решение владельца §93), а не заводить вторую ось: DRAFT ≙ ``provisional``,
ACTIVE ≙ ``approved``, добавлены ``candidate`` и ``retired``.

Узлы держат:

* умолчание по-прежнему ``provisional`` — новая строка сама себя не одобряет
  и не выводит;
* ``candidate`` — предложение, провенанса не требует;
* ``retired`` — решение, и провенанс у него тот же, что у ``approved``:
  дата, основание, «кто ИЛИ правило», правило с версией;
* значение вне четырёх в базу не попадает даже мимо ORM;
* дыра, которую вывод из оборота не закрывает сам — связь салона VERIFIED на
  выведенном каноне остаётся в подборе, — СЧЁТНАЯ: её видит
  ``check_canon_invariants`` и роняет его с ``--fail-on-violations``.
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

L = ServiceTemplate.Lifecycle
S = SalonService.MappingStatus


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Обёртывания CAT-2", slug="cat2-wraps")


@pytest.fixture
def human(db):
    return User.objects.create_user(username="cat2-human", password="x")


def _template(category, name="Канон CAT-2", **overrides):
    fields = dict(category=category, name=name, name_short=name[:40])
    fields.update(overrides)
    return ServiceTemplate.objects.create(**fields)


def _retired(category, human, name="Выведенный канон"):
    return _template(
        category,
        name=name,
        lifecycle=L.RETIRED,
        retired_by=human,
        retired_at=timezone.now(),
        retirement_source_ref="реестр владельца, строка 7",
    )


# ─── словарь и умолчание ─────────────────────────────────────────────────────


def test_the_axis_holds_the_contract_four_without_renaming_the_old_two() -> None:
    assert {(m.name, m.value) for m in L} == {
        ("PROVISIONAL", "provisional"),  # контракт: DRAFT
        ("CANDIDATE", "candidate"),
        ("APPROVED", "approved"),  # контракт: ACTIVE
        ("RETIRED", "retired"),
    }


def test_a_new_canon_is_still_provisional(category) -> None:
    assert _template(category).lifecycle == L.PROVISIONAL


def test_a_candidate_needs_no_provenance(category) -> None:
    tpl = _template(category, name="Кандидат", lifecycle=L.CANDIDATE)

    tpl.refresh_from_db()
    assert tpl.lifecycle == L.CANDIDATE


# ─── вывод из оборота — решение с провенансом ───────────────────────────────


def test_retired_without_any_basis_is_refused(category) -> None:
    with pytest.raises(IntegrityError, match="servicetemplate_retired_requires_provenance"):
        with transaction.atomic():
            _template(category, name="Голый вывод", lifecycle=L.RETIRED)


def test_retired_without_who_or_rule_is_refused(category) -> None:
    with pytest.raises(IntegrityError, match="servicetemplate_retired_requires_provenance"):
        with transaction.atomic():
            _template(
                category,
                name="Вывод без автора",
                lifecycle=L.RETIRED,
                retired_at=timezone.now(),
                retirement_source_ref="тикет",
            )


def test_who_and_rule_together_are_refused(category, human) -> None:
    with pytest.raises(IntegrityError, match="servicetemplate_retirement_is_who_xor_rule"):
        with transaction.atomic():
            _template(
                category,
                name="Вывод кто и правило",
                lifecycle=L.RETIRED,
                retired_by=human,
                retired_rule="sunset",
                retirement_rule_version="1",
                retired_at=timezone.now(),
                retirement_source_ref="тикет",
            )


def test_a_rule_without_version_is_refused(category) -> None:
    with pytest.raises(IntegrityError, match="servicetemplate_retirement_rule_carries_version"):
        with transaction.atomic():
            _template(
                category,
                name="Вывод правилом без версии",
                lifecycle=L.RETIRED,
                retired_rule="sunset",
                retired_at=timezone.now(),
                retirement_source_ref="тикет",
            )


def test_retired_by_a_person_with_basis_is_accepted(category, human) -> None:
    assert _retired(category, human).lifecycle == L.RETIRED


def test_retired_by_a_versioned_rule_is_accepted(category) -> None:
    tpl = _template(
        category,
        name="Вывод правилом",
        lifecycle=L.RETIRED,
        retired_rule="sunset",
        retirement_rule_version="1",
        retired_at=timezone.now(),
        retirement_source_ref="тикет",
    )

    assert tpl.lifecycle == L.RETIRED


def test_an_unknown_state_does_not_reach_the_database_even_past_the_orm(category) -> None:
    tpl = _template(category, name="Состояние вне оси")

    with pytest.raises(IntegrityError, match="servicetemplate_lifecycle_known"):
        with transaction.atomic():
            ServiceTemplate.objects.filter(pk=tpl.pk).update(lifecycle="active")


# ─── дыра, которую вывод не закрывает сам, — счётная ────────────────────────


def _verified_link(category, human, template, name):
    tenant = Tenant.objects.create(slug=f"cat2-{name[:10]}", name="Салон CAT-2")
    return SalonService.objects.create(
        tenant=tenant,
        category=category,
        template=template,
        name=name,
        duration_minutes=60,
        base_price=Decimal("2000"),
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор, строка 1",
    )


def _report(**options) -> str:
    out = StringIO()
    call_command("check_canon_invariants", stdout=out, **options)
    return out.getvalue()


def test_a_verified_link_on_a_retired_canon_is_counted(category, human) -> None:
    _verified_link(category, human, _retired(category, human), "Обёртывание выведено")

    report = _report()

    assert "verified_on_retired     : 1" in report, report
    assert "retired_without_basis   : 0" in report, report


def test_the_same_link_on_an_approved_canon_is_not_counted(category, human) -> None:
    approved = _template(
        category,
        name="Одобренный",
        lifecycle=L.APPROVED,
        approved_by=human,
        approved_at=timezone.now(),
        approval_source_ref="разбор",
    )
    _verified_link(category, human, approved, "Обёртывание действует")

    assert "verified_on_retired     : 0" in _report()


def test_the_retired_hole_fails_the_strict_run(category, human) -> None:
    _verified_link(category, human, _retired(category, human), "Обёртывание выведено строго")

    with pytest.raises(SystemExit) as exc:
        _report(fail_on_violations=True)
    assert exc.value.code == 1


def test_retired_without_basis_is_counted_when_the_schema_is_bypassed(category, human) -> None:
    """Положительная стража: счётчик видит то, что схема запрещает.

    В Postgres ``update()`` проверочное ограничение не обходит — база его
    держит. Чтобы счётчик не остался строкой, которую никто не видел
    ненулевой, ограничение снимается внутри транзакции теста: DDL в
    Postgres транзакционен, и откат теста возвращает его на место.
    """
    from django.db import connection

    # Сначала DDL, потом строка: вставка оставляет отложенные триггеры
    # внешних ключей, и ALTER той же таблицы после неё в той же транзакции
    # Postgres отвергает («pending trigger events»).
    constraint = next(
        c
        for c in ServiceTemplate._meta.constraints
        if c.name == "servicetemplate_retired_requires_provenance"
    )
    with connection.schema_editor() as editor:
        editor.remove_constraint(ServiceTemplate, constraint)
    _template(
        category,
        name="Вывод без основания",
        lifecycle=L.RETIRED,
        retired_by=human,
        retired_at=timezone.now(),
    )

    assert "retired_without_basis   : 1" in _report()


def test_a_candidate_is_queued(category) -> None:
    _template(category, name="Кандидат в очереди", lifecycle=L.CANDIDATE)

    assert "candidate_templates     : 1" in _report()
