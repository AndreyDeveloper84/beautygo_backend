"""Имя часового пояса мастера — только настоящее имя IANA (раздел Q, P2 владельца).

Проверка — точное членство в ``zoneinfo.available_timezones()``, а не «удалось
ли построить ``ZoneInfo``». ``zoneinfo`` читает файлы. На Windows (файловая
система без учёта регистра + пакет tzdata) ``ZoneInfo("europe/moscow")`` может
загрузиться, а на Linux — нет. Проверка через построение приняла бы на одной
машине то, что отвергнет другая. Членство одинаково везде и так же отвергает
пустую строку и ключи с ``..``.

Набор имён строится один раз на процесс: список поясов меняется только с
обновлением tzdata, то есть с перезапуском.
"""
from __future__ import annotations

from functools import lru_cache
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError


@lru_cache(maxsize=1)
def _iana_names() -> frozenset[str]:
    return frozenset(available_timezones())


def is_iana_timezone(value: object) -> bool:
    """Является ли значение точным именем пояса IANA."""
    return isinstance(value, str) and value in _iana_names()


def validate_iana_timezone(value: object) -> None:
    """Валидатор поля: отказ с именем значения и примером."""
    if not is_iana_timezone(value):
        raise ValidationError(
            "«%(value)s» — не имя часового пояса IANA. Пример: Europe/Moscow.",
            code="invalid_timezone",
            params={"value": value},
        )
