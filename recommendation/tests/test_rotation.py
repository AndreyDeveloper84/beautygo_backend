"""S6 — ротация. Контракт §12, решение владельца §29.6.

Два свойства, и второе человек замечает первым:

* внутри одного контекста порядок **стабилен** — список не перетасовывается
  между репликами одного человека;
* между контекстами первое место распределено **равномерно** — ни один
  мастер не оказывается системно ниже из-за фамилии.
"""
from __future__ import annotations

import uuid

from recommendation._rotation import rotate_within_tier, rotation_key


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i) for i in range(1, n + 1)]


def test_same_seed_gives_same_order():
    ids = _ids(8)
    assert rotate_within_tier(ids, "conv-1") == rotate_within_tier(ids, "conv-1")


def test_order_does_not_depend_on_incoming_order():
    """Стабильность — свойство пары (seed, id), а не входного порядка."""
    ids = _ids(8)
    assert rotate_within_tier(ids, "conv-1") == rotate_within_tier(list(reversed(ids)), "conv-1")


def test_different_seeds_give_different_first_place():
    """Между людьми первое место расходится — иначе экспозиция системно смещена."""
    ids = _ids(8)
    firsts = {rotate_within_tier(ids, f"conv-{i}")[0] for i in range(40)}
    assert len(firsts) > 1


def test_no_seed_means_undefined_order_not_alphabet():
    """§12.3: нет seed — ротация НЕ применяется, и это не значит «алфавит».

    В оригинале (`discovery.py`) `rotation_seed=None` возвращал порядок
    `("name", "id")`, то есть ровно тот лексикографический fallback, который
    канон §9.1 запрещает при отсечении. Здесь отсутствие seed означает
    «различить нечем», и порядок остаётся тем, что пришёл: сортировать его
    было бы утверждением о превосходстве, которого никто не делал.
    """
    ids = _ids(5)
    shuffled = [ids[3], ids[0], ids[4], ids[1], ids[2]]
    assert rotate_within_tier(shuffled, None) == shuffled


def test_rotation_key_is_pure():
    """Ни часов, ни `random`: ключ — функция пары и ничего больше."""
    cid = uuid.uuid4()
    assert rotation_key("seed", cid) == rotation_key("seed", cid)
    assert rotation_key("seed-a", cid) != rotation_key("seed-b", cid)
