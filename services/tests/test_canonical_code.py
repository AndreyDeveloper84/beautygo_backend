"""``ServiceTemplate.canonical_code`` — контракт идентичности и fail-closed bootstrap (MAP-AUTO-01/02a, OD-MAP-05).

Что стережётся:

* формат ``N.N.N``; уникальность среди непустых (IntegrityError); ``NULL``
  допускает сколько угодно строк;
* **неизменяемость после установки** — и через ``full_clean()`` (форма), и
  через ``save()``; пустой код заполнить можно; ``name`` меняется — код нет;
* ``seed_service_templates`` (DRF-196) оставляет код ``NULL``;
* bootstrap на фикстуре «3 строки seed + 1 чужая»: три кода поставлены,
  чужая ``NULL``, отчёт считает всё четырьмя счётчиками; повторный прогон —
  0 новых; ``dry_run`` не пишет;
* **отказ без записи**: строка уже несёт другой код, чем seed для её пары →
  ``BootstrapRefused`` и ни одна другая строка не тронута; seed с дублем
  кода/пары — отказ до чтения базы;
* обратный ход снимает только коды из seed — чужой код остаётся;
* сама миграция ``0023`` ссылается на эти функции (а не дублирует логику).
"""
from __future__ import annotations

import ast
import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction

from services.canonical_code import (
    SEED_PATH,
    BootstrapRefused,
    bootstrap_canonical_codes,
    seed_pairs,
    unbootstrap_canonical_codes,
    validate_canonical_code,
)
from services.models import ServiceCategory, ServiceTemplate

pytestmark = pytest.mark.django_db


def _cat(name="Базовый ручной массаж"):
    return ServiceCategory.objects.get_or_create(name=name)[0]


def _tpl(name, code=None, cat=None):
    return ServiceTemplate.objects.create(
        category=cat or _cat(), name=name, name_short=name[:40], canonical_code=code,
    )


# ---------------------------------------------------------------- контракт поля

def test_format_is_n_dot_n_dot_n():
    for bad in ("1.1", "a.b.c", "1.1.1.1", " 1.1.1", "1-1-1"):
        with pytest.raises(ValidationError):
            validate_canonical_code(bad)
    validate_canonical_code("1.3.24")
    validate_canonical_code(None)
    validate_canonical_code("")


def test_code_is_unique_among_non_null_and_null_is_free():
    _tpl("Массаж спины", "1.1.4")
    _tpl("Без кода 1")
    _tpl("Без кода 2")            # два NULL — законно
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _tpl("Дубль кода", "1.1.4")


def test_code_is_immutable_once_set_via_clean_and_via_save():
    tpl = _tpl("Массаж спины", "1.1.4")
    tpl.canonical_code = "1.1.5"
    with pytest.raises(ValidationError, match="неизменяем"):
        tpl.full_clean()
    with pytest.raises(ValidationError, match="неизменяем"):
        tpl.save()
    tpl.canonical_code = None                        # снять тоже нельзя
    with pytest.raises(ValidationError, match="неизменяем"):
        tpl.save()
    tpl.refresh_from_db()
    assert tpl.canonical_code == "1.1.4"


def test_empty_code_can_be_filled_and_rename_keeps_the_code():
    tpl = _tpl("Массаж спины")
    tpl.canonical_code = "1.1.4"
    tpl.full_clean()
    tpl.save()
    tpl.name = "Массаж спины (переименован)"
    tpl.save()
    tpl.refresh_from_db()
    assert tpl.canonical_code == "1.1.4" and tpl.name.endswith("(переименован)")


def test_drf196_seed_templates_stay_without_code():
    call_command("seed_service_templates", stdout=StringIO())
    assert ServiceTemplate.objects.count() > 0
    assert not ServiceTemplate.objects.filter(canonical_code__isnull=False).exists()


# ---------------------------------------------------------------- bootstrap

def _fixture():
    """Три строки seed (по паре, без кода) + одна чужая."""
    pairs = seed_pairs(SEED_PATH)
    wanted = {"1.1.4", "1.1.5", "1.3.24"}
    for (cat_name, service), code in pairs.items():
        if code in wanted:
            _tpl(service, None, _cat(cat_name))
    foreign = _tpl("Чужой канон DRF-196", None, _cat("Чужая категория"))
    return foreign


def test_bootstrap_assigns_codes_by_pair_and_leaves_foreign_null():
    foreign = _fixture()
    report = bootstrap_canonical_codes(ServiceTemplate)
    assert (report.assigned, report.already_coded, report.foreign) == (3, 0, 1)
    assert report.missing_in_db == report.seed_pairs - 3
    assert report.templates_total == 4
    assert set(ServiceTemplate.objects.exclude(canonical_code=None).values_list("canonical_code", flat=True)) == {
        "1.1.4", "1.1.5", "1.3.24",
    }
    foreign.refresh_from_db()
    assert foreign.canonical_code is None

    again = bootstrap_canonical_codes(ServiceTemplate)      # идемпотентно
    assert (again.assigned, again.already_coded) == (0, 3)


def test_dry_run_writes_nothing():
    _fixture()
    report = bootstrap_canonical_codes(ServiceTemplate, dry_run=True)
    assert report.assigned == 3 and {c for c, _ in report.assignments} == {"1.1.4", "1.1.5", "1.3.24"}
    assert not ServiceTemplate.objects.filter(canonical_code__isnull=False).exists()


def test_conflicting_existing_code_refuses_without_writing_anything():
    _fixture()
    # база спорит с seed
    ServiceTemplate.objects.filter(name="Массаж спины").update(canonical_code="9.9.9")
    with pytest.raises(BootstrapRefused) as exc:
        bootstrap_canonical_codes(ServiceTemplate)
    assert "в базе код 9.9.9, seed говорит 1.1.4" in str(exc.value)
    # ни одна ДРУГАЯ строка кода не получила — отказ до первой записи
    coded = set(ServiceTemplate.objects.exclude(canonical_code=None).values_list("canonical_code", flat=True))
    assert coded == {"9.9.9"}


def test_seed_that_is_not_a_bijection_is_refused_before_touching_the_db(tmp_path):
    rows = json.loads(SEED_PATH.read_text(encoding="utf-8"))[:3]
    rows[1]["code"] = rows[0]["code"]                        # дубль кода
    bad = tmp_path / "seed.json"
    bad.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BootstrapRefused, match="повторяется"):
        seed_pairs(bad)
    rows = json.loads(SEED_PATH.read_text(encoding="utf-8"))[:3]
    rows[1]["service"] = rows[0]["service"]
    rows[1]["subcategory"] = rows[0]["subcategory"]   # дубль пары
    bad.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BootstrapRefused, match="пара .* повторяется"):
        seed_pairs(bad)


def test_the_real_seed_is_a_bijection():
    pairs = seed_pairs(SEED_PATH)
    assert len(pairs) == 1223 and len(set(pairs.values())) == 1223


def test_full_seed_bootstraps_1223_of_1223_and_the_forty_drf196_rows_stay_null():
    """Копия пилотной схемы: seed_canonical_catalog (1223 без кода) + 40 строк
    DRF-196 → dry-run 1223/1223, чужие 40 — NULL, пар без строки — 0."""
    call_command("seed_service_templates", stdout=StringIO())
    call_command("seed_canonical_catalog", stdout=StringIO())
    total = ServiceTemplate.objects.count()
    report = bootstrap_canonical_codes(ServiceTemplate, dry_run=True)
    assert (report.assigned, report.missing_in_db) == (1223, 0)
    assert report.foreign == total - 1223 > 0
    report = bootstrap_canonical_codes(ServiceTemplate)
    assert ServiceTemplate.objects.filter(canonical_code__isnull=False).count() == 1223
    assert ServiceTemplate.objects.get(canonical_code="1.3.24").requires_health_check is True


def test_unbootstrap_removes_only_seed_codes():
    _fixture()
    bootstrap_canonical_codes(ServiceTemplate)
    # чужой код — не из seed
    ServiceTemplate.objects.filter(name="Чужой канон DRF-196").update(canonical_code="7.7.7")
    assert unbootstrap_canonical_codes(ServiceTemplate) == 3
    left = list(ServiceTemplate.objects.exclude(canonical_code=None).values_list("canonical_code", flat=True))
    assert left == ["7.7.7"]


def test_migration_0023_delegates_to_the_shared_functions():
    mig = Path(__file__).resolve().parents[1] / "migrations" / "0023_canonical_code_bootstrap.py"
    src = mig.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert {"bootstrap_canonical_codes", "unbootstrap_canonical_codes"} <= calls
    # миграция не дублирует логику: ни фильтра по паре, ни update() кодов внутри неё самой
    writes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func).endswith((".update", ".bulk_update"))
    ]
    assert not writes
