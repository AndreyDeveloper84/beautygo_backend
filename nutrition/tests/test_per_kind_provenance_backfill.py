"""Правило по-видового backfill — решение владельца 16.09.2026 (DRF-1929).

Заменяет узел-заглушку ``test_per_kind_provenance_backfill_decision``,
который держал вопрос открытым, пока умолчание не было выбрано. Ответ
получен, вопрос закрыт, и вместо отсутствия решения здесь проверяется
САМО решение.

**Что проверяется — правило, а не применение миграции.** В репозитории
нет ``django_test_migrations``, а единственный executor-тест
(``payments/tests/test_table_rename_migration.py``) идёт только под
Postgres и вне его молча пропускается, то есть локально дал бы зелень,
ничего не проверив. Поэтому правило вынесено из ``0023`` отдельной
функцией и проверяется напрямую. **Названный предел:** что миграция
действительно доехала до строк, этими узлами НЕ доказано.
"""
from __future__ import annotations

import importlib
import inspect

from nutrition.models import NutritionProfile

# Имя модуля миграции начинается с цифр, поэтому обычный ``import`` для
# него невозможен — только ``import_module`` по строке.
migration = importlib.import_module(
    "nutrition.migrations.0023_per_kind_provenance_legacy_backfill"
)


def test_the_rule_names_the_kind_that_has_a_number():
    """Число у вида есть — ``unknown_legacy``; нет — ``none``."""
    assert migration.kind_backfill_value(True) == "unknown_legacy", (
        "у вида с числом происхождение «не сохранялось», а не «его нет»"
    )
    assert migration.kind_backfill_value(False) == "none", (
        "у вида без числа происхождению нечему принадлежать (0017)"
    )


def test_both_names_are_real_choices_of_the_field():
    """Оба имени — законные значения колонок, а не свободные строки."""
    valid = {value for value, _label in NutritionProfile.TargetsSource.choices}
    assert migration.kind_backfill_value(True) in valid
    assert migration.kind_backfill_value(False) in valid


def test_the_rule_does_not_read_the_old_whole_set_signature():
    """Запрет владельца: старый ``targets_source`` НЕ копируется.

    Отрицательный контроль на СОСТАВ правила, а не на его результат:
    результат совпал бы случайно у строки, где общая подпись и так
    ``unknown_legacy``. Здесь проверяется, что вид определяется наличием
    ЧИСЛА, и ``targets_source`` в отборе не участвует вовсе.
    """
    source = inspect.getsource(migration.name_per_kind_provenance)
    assert "targets_source" not in source, (
        "правило читает общую подпись — это прямо запрещено решением "
        "владельца: она про НАБОР, а не про вид, и копирование размножило "
        "бы исходную ошибку F1(б)"
    )
    assert "daily_kcal" in source and "daily_water_ml" in source, (
        "отбор обязан идти по наличию числа каждого вида"
    )


def test_schema_still_writes_nothing_by_default():
    """Положительный контроль ``0022``: у колонок нет умолчания.

    Появись умолчание — DDL проставил бы значение КАЖДОЙ существующей
    строке, и данные оказались бы тронуты схемной миграцией, а не
    ``0023``, где это решение названо и обратимо.
    """
    for name in ("calories_source", "fluids_source"):
        field = NutritionProfile._meta.get_field(name)
        assert field.null is True, f"{name}: ожидался null=True"
        assert not field.has_default(), (
            f"{name}: у поля появилось умолчание — значит схемная миграция "
            "пишет в существующие строки"
        )
