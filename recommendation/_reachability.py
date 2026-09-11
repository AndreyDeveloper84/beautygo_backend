"""Аналитическая достижимость: кого не видит никто. Задача T5 (DRF-1566).

Зачем это существует
--------------------
`apps/marketplace/discovery.py` — 2402 строки, настоящая точка решения,
и в ней **ноль вхождений `logger`**; объект `Recommendation` не создаётся
нигде (P1-R1). Экспозиционное смещение эмпирически неизмеримо: нет ни
журнала показов, ни идентификатора рекомендации, по которому его можно
было бы восстановить.

Обход не требует ни логов, ни трафика: **функция ранжирования
детерминирована и чиста**, поэтому недостижимость **вычисляется**.
Не оценивается по выборке, не набирается статистикой — доказывается.

Что именно доказывается
-----------------------
Только **провably недостижимые**. Утверждение «этот кандидат достижим»
про хеш-ротацию потребовало бы говорить о сюръективности blake2b на
перестановках яруса — это правдоподобно, но не доказано, и здесь такие
утверждения не делаются. Обратное — доказуемо арифметикой:

* **порядок с ротацией внутри яруса** (резолвер, S6): кандидат не может
  попасть в первые `k` **ни при каком** seed тогда и только тогда, когда
  кандидатов в строго более высоких ярусах уже `k` или больше. Ротация
  переставляет только внутри яруса и не может обогнать стадию, которая
  различила (§12.2);
* **порядок с детерминированной ничьёй** (сегодняшний домашний экран:
  `sort(key=(-score, str(id)))[:3]`): кандидат недостижим, если его
  позиция в этом единственном порядке `>= k`. Здесь недостижимость
  жёстче — она **одинакова для всех людей и на все времена**: мастер
  с «неудачным» UUID при равенстве не показывается никому и никогда.

Разница между двумя множествами и есть цена лексикографической ничьи под
отсечением, выраженная числом, — то, что канон §9.1 запрещает словами.

Как это доказывает, что авторитет один
--------------------------------------
Если две поверхности на один и тот же запрос дают **разные** множества
недостижимых, авторитета всё ещё два — сколько бы общих модулей они ни
импортировали. Это доказательство сильнее структурного гарда: гард
показывает, что второго места решения нет в коде, достижимость — что его
нет **в следствиях**.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Mapping, Sequence


@dataclass(frozen=True)
class ReachabilityReport:
    """Результат замера. Числа и списки, без интерпретаций."""

    model: str
    k: int
    total: int
    unreachable: tuple[Hashable, ...]

    @property
    def unreachable_count(self) -> int:
        return len(self.unreachable)

    @property
    def unreachable_share(self) -> float:
        return (self.unreachable_count / self.total) if self.total else 0.0

    def summary(self) -> str:
        return (
            f"{self.model}: {self.unreachable_count} из {self.total} "
            f"недостижимы при k={self.k} ({self.unreachable_share:.0%})"
        )


def unreachable_with_rotation(
    tiers: Mapping[Hashable, int], *, k: int,
) -> ReachabilityReport:
    """Ярусный порядок с ротацией внутри яруса — модель резолвера.

    Кандидат провably недостижим ⟺ строго выше него уже `k` кандидатов.
    Ротация тут ничего не меняет по построению: она работает **только
    внутри яруса** и не может поднять кандидата над теми, кого стадия
    различила (§12.1, позитивная стража DRF-1411).
    """
    if k < 1:
        raise ValueError("k — сколько будет показано; k < 1 не является запросом")
    counts_by_tier: dict[int, int] = {}
    for tier in tiers.values():
        counts_by_tier[tier] = counts_by_tier.get(tier, 0) + 1

    ordered_tiers = sorted(counts_by_tier)
    above: dict[int, int] = {}
    running = 0
    for tier in ordered_tiers:
        above[tier] = running
        running += counts_by_tier[tier]

    unreachable = tuple(
        sorted(
            (cid for cid, tier in tiers.items() if above[tier] >= k),
            key=str,
        )
    )
    return ReachabilityReport("rotation_within_tier", k, len(tiers), unreachable)


def unreachable_with_fixed_order(
    ordered_ids: Sequence[Hashable], *, k: int,
) -> ReachabilityReport:
    """Единственный детерминированный порядок — модель сегодняшнего экрана.

    Ничья разводится чистой функцией идентификатора (`str(id)`, алфавит,
    UUID), а срез стоит после неё. Значит порядок один на всех людей, и
    всё, что за позицией `k`, не видит **никто и никогда**.
    """
    if k < 1:
        raise ValueError("k — сколько будет показано; k < 1 не является запросом")
    unreachable = tuple(ordered_ids[k:])
    return ReachabilityReport("fixed_tie_break", k, len(ordered_ids), unreachable)


def exposure_gap(
    *, rotation: ReachabilityReport, fixed: ReachabilityReport,
) -> tuple[Hashable, ...]:
    """Кого теряет детерминированная ничья и сохраняет ротация.

    Это и есть цена запрета из канона §9.1, выраженная поимённо: люди,
    которых сегодня не видит никто, а после границы увидит кто-то.
    """
    return tuple(sorted(set(fixed.unreachable) - set(rotation.unreachable), key=str))
