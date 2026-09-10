"""Жизненный цикл канонической услуги (§93).

Справочник перестал быть замороженным закрытым списком: нет подходящего
канона — оператор заводит новый со статусом `provisional`, и после
проверки тот становится `approved`. Без этого разбор упирается ровно в
то, во что упёрся на пилоте: две услуги эпиляции из семнадцати не легли
ни на один существующий канон, и завести им канон было негде.

Ось у этого состояния своя, и путать её со связью нельзя::

    SalonService.MappingStatus  «чем доказано, что вот эта услуга салона —
                                 вот этот канон»
    ServiceTemplate.Lifecycle   «проверял ли кто-нибудь, что вот этот канон
                                 вообще должен существовать»

**Чего этот срез сознательно НЕ делает.** Он не гейтит подбор по
`lifecycle`. Сегодня связь `VERIFIED` на черновом каноне в подбор
попадает, и это открытый вопрос владельцу, упирающийся в два других
его же незакрытых («кто оператор» и «что значит после проверки»).
Второй гейт, заведённый по чужой догадке, тише и опаснее отсутствующего:
пустая полка выглядит одинаково, а причину искать негде.

Дыра при этом не молчит — её считает `check_canon_invariants`, и
последний тест файла проверяет, что считает именно её.
"""
from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.models import (
    SalonService,
    ServiceCategory,
    ServiceTemplate,
)
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

L = ServiceTemplate.Lifecycle
S = SalonService.MappingStatus


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Пилинги", slug="life-peel")


@pytest.fixture
def human(db):
    return User.objects.create_user(username="life-human", password="x")


def _template(category, name="Новый канон", **overrides):
    fields = dict(category=category, name=name, name_short=name[:40])
    fields.update(overrides)
    return ServiceTemplate.objects.create(**fields)


def _approved(category, human, name="Одобренный канон"):
    return _template(
        category, name=name,
        lifecycle=L.APPROVED,
        approved_by=human,
        approved_at=timezone.now(),
        approval_source_ref="разбор 56 услуг, строка 3",
    )


# ---------------------------------------------------------------------------
# Умолчание
# ---------------------------------------------------------------------------


def test_new_canon_is_provisional_not_approved(category):
    """Новый канон черновой, и это не придирка к слову.

    Канон, заведённый кодом, миграцией или чужой рукой, никем не
    проверен по определению. Умолчание `APPROVED` означало бы, что
    каждая новая строка сама себя одобрила — то же самое, что §90
    запретил делать с признаком здоровья.
    """
    assert _template(category).lifecycle == L.PROVISIONAL


# ---------------------------------------------------------------------------
# Провенанс одобрения
# ---------------------------------------------------------------------------


def test_approved_without_basis_is_refused(category, human):
    """Одобрение без основания невозможно на уровне схемы."""
    with pytest.raises(IntegrityError, match="approved_requires_provenance"):
        with transaction.atomic():
            _template(
                category, name="Без основания",
                lifecycle=L.APPROVED,
                approved_by=human,
                approved_at=timezone.now(),
            )


def test_approved_without_who_or_rule_is_refused(category):
    """И без автора, и без правила — тоже."""
    with pytest.raises(IntegrityError, match="approved_requires_provenance"):
        with transaction.atomic():
            _template(
                category, name="Без автора",
                lifecycle=L.APPROVED,
                approved_at=timezone.now(),
                approval_source_ref="откуда-то",
            )


def test_who_and_rule_together_are_refused(category, human):
    """Кто ИЛИ правило, но не оба — как у связи и как у синонима."""
    with pytest.raises(IntegrityError, match="approval_is_who_xor_rule"):
        with transaction.atomic():
            _template(
                category, name="И кто, и правило",
                lifecycle=L.APPROVED,
                approved_by=human,
                approved_rule="seed_canonical_catalog",
                approval_rule_version="v1",
                approved_at=timezone.now(),
                approval_source_ref="ref",
            )


def test_rule_without_version_is_refused(category):
    """Правило без версии — «одобрено какой-то из версий»."""
    with pytest.raises(IntegrityError, match="approval_rule_carries_version"):
        with transaction.atomic():
            _template(
                category, name="Правило без версии",
                lifecycle=L.APPROVED,
                approved_rule="seed_canonical_catalog",
                approved_at=timezone.now(),
                approval_source_ref="ref",
            )


def test_provisional_needs_no_basis(category):
    """Черновому канону провенанс не нужен — и это важно.

    Требуй схема основание у каждой строки, оператор не смог бы завести
    канон на ходу, а ровно ради этого §93 и открыл справочник.
    """
    assert _template(category, name="Черновой").lifecycle == L.PROVISIONAL


def test_approved_with_full_basis_is_accepted(category, human):
    """Положительная стража: правильно оформленное одобрение проходит.

    Без неё четыре проверки выше зеленели бы и на схеме, которая
    запрещает одобрение вовсе.
    """
    assert _approved(category, human).lifecycle == L.APPROVED


# ---------------------------------------------------------------------------
# Дедовщина: что именно утверждает засыпка
# ---------------------------------------------------------------------------


def test_grandfathering_rule_does_not_claim_a_human_checked():
    """Правило засыпки названо буквальным и не врёт.

    `grandfathered_before_lifecycle` утверждает ровно то, что истинно по
    построению: строка существовала до появления жизненного цикла. Имя
    вроде `owner_reference_catalog` было бы сильнее и неправдой —
    шаблоны создаются двумя разными командами, и на произвольной базе
    засыпка накрывает обе.

    Тест сторожит имя, потому что «усилить» его при следующей правке
    ничего не стоит, а проверить потом будет нечем.
    """
    import importlib

    migration = importlib.import_module(
        "services.migrations.0021_template_lifecycle",
    )
    assert migration.GRANDFATHER_RULE == "grandfathered_before_lifecycle"
    assert "НЕ означает" in migration.GRANDFATHER_REF, (
        "основание засыпки перестало оговаривать, что проверки человеком не было"
    )


def test_seeded_catalog_is_approved_by_rule_with_provenance(category):
    """Эталонный справочник одобряется правилом, а не остаётся черновым.

    Иначе повторный прогон сида оставлял бы весь канонический каталог
    непроверенным, и очередь одобрения куратора состояла бы из тысячи
    с лишним строк, которые он не заводил.
    """
    out = StringIO()
    call_command("seed_canonical_catalog", stdout=out)

    seeded = ServiceTemplate.objects.filter(approved_rule="seed_canonical_catalog")
    assert seeded.exists(), "сид не проставил правило одобрения"
    row = seeded.first()
    assert row.lifecycle == L.APPROVED
    assert row.approved_by_id is None, "правило и человек одновременно"
    assert row.approval_rule_version, "версия правила пуста"
    assert row.approval_source_ref, "основание пусто"


# ---------------------------------------------------------------------------
# Дыра, которую решено не закрывать, обязана быть счётной
# ---------------------------------------------------------------------------


def test_detector_counts_verified_link_on_a_provisional_canon(category, human, db):
    """Счётчик видит связь `VERIFIED` на черновом каноне.

    Это и есть незакрытый вопрос владельцу в виде числа. Пока гейта
    нет, разница между «мы решили не закрывать» и «мы не заметили» —
    ровно в наличии этой строки.
    """
    tenant = Tenant.objects.create(slug="life-tenant", name="Салон")
    provisional = _template(category, name="Черновой канон")
    SalonService.objects.create(
        tenant=tenant, category=category, template=provisional,
        name="Услуга на черновом каноне",
        duration_minutes=60, base_price=Decimal("2000"),
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор 56 услуг, строка 9",
    )

    out = StringIO()
    call_command("check_canon_invariants", stdout=out)
    report = out.getvalue()

    assert "verified_on_provisional : 1" in report, report
    assert "approved_without_basis  : 0" in report, report


def test_detector_reports_zero_when_the_canon_is_approved(category, human, db):
    """Положительная стража счётчика: на одобренном каноне — ноль.

    Без неё предыдущий тест зеленел бы и на счётчике, который всегда
    возвращает единицу.
    """
    tenant = Tenant.objects.create(slug="life-tenant-2", name="Салон 2")
    approved = _approved(category, human)
    SalonService.objects.create(
        tenant=tenant, category=category, template=approved,
        name="Услуга на одобренном каноне",
        duration_minutes=60, base_price=Decimal("2000"),
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор 56 услуг, строка 10",
    )

    out = StringIO()
    call_command("check_canon_invariants", stdout=out)

    assert "verified_on_provisional : 0" in out.getvalue()


def test_detector_prints_the_subject_next_to_the_result(category, db):
    """Рядом с числами напечатан предмет.

    Ноль без предмета неотличим от «посчитали не то»: пустая таблица,
    тенантный менеджер вне контекста запроса, опечатка в фильтре — всё
    это даёт тот же ноль, что и отсутствие нарушений.
    """
    _template(category, name="Хоть что-то")
    out = StringIO()
    call_command("check_canon_invariants", stdout=out)
    report = out.getvalue()

    assert "предмет:" in report
    assert "ServiceTemplate=" in report
    assert "ServiceTemplate=0" not in report, "счётчик смотрел на пустую таблицу"


def _migration_0021():
    import importlib

    return importlib.import_module("services.migrations.0021_template_lifecycle")


def test_grandfathering_approves_existing_rows(category, db):
    """Засыпка переводит существующие строки в одобренные.

    Проверяется вызовом функции миграции, а не накаткой: тестовая база
    строится с нуля, на ней засыпать нечего, и без этого теста код
    засыпки не исполнился бы ни разу.
    """
    _template(category, name="Существовала до цикла")
    migration = _migration_0021()

    from django.apps import apps as django_apps

    before = ServiceTemplate.objects.filter(lifecycle=L.APPROVED).count()
    migration.grandfather_existing(django_apps, None)

    assert ServiceTemplate.objects.filter(lifecycle=L.APPROVED).count() == before + 1
    row = ServiceTemplate.objects.get(name="Существовала до цикла")
    assert row.approved_rule == migration.GRANDFATHER_RULE
    assert row.approved_by_id is None, "засыпка приписала одобрение человеку"
    assert row.approved_at is not None


def test_ungrandfathering_touches_only_its_own_rows(category, human, db):
    """Откат снимает одобрение только своё, а не человеческое.

    Иначе откат миграции стирал бы работу куратора, сделанную после
    накатки, — тем самым механизмом, который заведён её сохранять.
    """
    migration = _migration_0021()
    from django.apps import apps as django_apps

    _template(category, name="Дедовская")
    migration.grandfather_existing(django_apps, None)
    by_human = _approved(category, human, name="Одобрена человеком")

    migration.ungrandfather(django_apps, None)

    assert ServiceTemplate.objects.get(name="Дедовская").lifecycle == L.PROVISIONAL
    by_human.refresh_from_db()
    assert by_human.lifecycle == L.APPROVED, "откат снял человеческое одобрение"
    assert by_human.approved_by_id == human.pk
