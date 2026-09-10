"""Новое состояние связи нельзя завести молча (§76, §93).

Этот файл — сторож, а не проверка конкретного значения. Он написан
**до** четвёртого состояния и обязан краснеть на любом пятом, шестом и
далее, если его заведут в модели и забудут научить остальных.

Почему без него нельзя
----------------------

``_MappingFacts._as_status`` (``users/recommendation_source.py``) читает
значение из базы и переводит его в значение контракта. Незнакомое
значение он возвращает как ``UNMAPPED`` — **намеренно**, fail-closed:
неизвестное не толкуется в пользу допуска.

У этой правильной осторожности есть тихая цена. Состояние, добавленное в
``SalonService.MappingStatus`` и не добавленное сюда, читается доменом
как «связи ещё нет» — и попадает в счётчик ``unmapped``. Для четвёртого
состояния, «не подлежит рекомендациям», это ровно та подмена, которую
§93 запрещает: **отказ схлопывается обратно в отсутствие**, а
«решено, что связи не будет» становится неотличимо от «про строку ещё
никто ничего не сказал». И произойдёт это без единой красной строки:
гейт по-прежнему не пропустит такую услугу в подбор, наружу поведение не
изменится, и увидеть подмену будет негде, кроме переписи.

Три проверки ниже закрывают три места, где новое состояние теряется:
перевод, ранжирование и перепись. Каждая сформулирована как правило над
**всеми** значениями перечисления, а не как утверждение про одно из них,
потому что сторож, знающий только сегодняшнее значение, завтрашнего не
поймает.

Проверено подменой: на добавленном в модель ``not_recommendable`` без
правки резолвера первый тест краснеет.
"""
from __future__ import annotations

import dataclasses

from recommendation.api import MappingCensus, MappingStatus
from services.models import SalonService
from users.recommendation_source import _MappingFacts

#: Единственное значение модели, которому РАЗРЕШЕНО читаться как
#: `UNMAPPED`. Все остальные, прочитанные так, — потерянные.
THE_ONLY_UNMAPPED = SalonService.MappingStatus.UNMAPPED.value


def test_every_model_status_is_taught_to_the_resolver():
    """Ни одно состояние модели не читается доменом как «связи ещё нет».

    ``_as_status`` отдаёт `UNMAPPED` на всё, чего не знает. Значит
    состояние, забытое в резолвере, неотличимо от отсутствия связи — и
    отличить его нельзя ничем, кроме этой проверки.
    """
    collapsed = [
        status.value
        for status in SalonService.MappingStatus
        if status.value != THE_ONLY_UNMAPPED
        and _MappingFacts._as_status(status.value) is MappingStatus.UNMAPPED
    ]
    assert not collapsed, (
        "состояния модели читаются доменом как UNMAPPED, то есть потеряны: "
        + ", ".join(sorted(collapsed))
        + ". Добавьте их в recommendation MappingStatus и в _as_status."
    )


def test_model_statuses_do_not_share_a_domain_value():
    """Два состояния модели не схлопываются в одно значение контракта.

    Проверка шире предыдущей: она ловит не только потерю в `UNMAPPED`,
    но и любое склеивание — например, если новое состояние поленятся
    заводить и отдадут как `REVIEW_REQUIRED`. Разные состояния обязаны
    оставаться различимыми на всём пути до переписи.
    """
    seen: dict[MappingStatus, str] = {}
    clashes: list[str] = []
    for status in SalonService.MappingStatus:
        domain = _MappingFacts._as_status(status.value)
        if domain in seen:
            clashes.append(f"{seen[domain]} и {status.value} -> {domain}")
        seen[domain] = status.value
    assert not clashes, "состояния модели склеены в одно: " + "; ".join(clashes)


def test_every_domain_status_has_a_rank():
    """У каждого значения контракта есть место в порядке «лучшести».

    ``_RANK`` спрашивается по ключу в ``max(...)``: значение без записи
    роняет подбор ``KeyError``'ом, причём не на добавлении, а на первой
    живой строке с этим статусом.
    """
    missing = [s.value for s in MappingStatus if s not in _MappingFacts._RANK]
    assert not missing, "нет записи в _RANK: " + ", ".join(sorted(missing))


#: Единственное состояние без собственной колонки переписи — и это не
#: недосмотр: гейт допускает ровно `VERIFIED`, поэтому его роль играет
#: `recommendation_eligible`.
#:
#: Оговорка, которую стоит держать на виду: это НЕ одно и то же число.
#: `recommendation_eligible` — те, кто прошёл ВЕСЬ S1, то есть `VERIFIED`
#: минус выбывшие раньше по безопасности, неактивности и способности.
#: Значит «подтверждено, но не показано» перепись сегодня показать не
#: может. Здесь это не чинится: слой переписи не мой срез. Но и
#: притворяться, что колонка есть, нельзя.
COUNTED_BY_THE_GATE_ITSELF = {MappingStatus.VERIFIED}


def test_every_domain_status_has_its_own_census_counter():
    """У каждого значения контракта своя колонка в переписи.

    Перепись — единственное, по чему видно движение разметки (§76).
    Состояние без своей колонки либо не считается вовсе, либо
    приплюсовывается к чужой, и тогда числа врут в ту же сторону, что и
    потерянный статус: отказ читается как отсутствие.
    """
    counters = {f.name for f in dataclasses.fields(MappingCensus)}
    missing = [
        s.value for s in MappingStatus
        if s not in COUNTED_BY_THE_GATE_ITSELF and s.value.lower() not in counters
    ]
    assert not missing, (
        "нет своей колонки в MappingCensus: " + ", ".join(sorted(missing))
        + ". Иначе состояние посчитается в чужой."
    )
