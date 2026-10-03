"""Перенос возможностей в общий словарь — общее для миграции и теста (DRF-2743).

Модуль лежит в пакете миграций намеренно, как ``_drf2742_evidence_kind_demote``:
логика принадлежит миграции ``0040``, имя на ``_`` загрузчик Django
миграцией не считает.

Зачем шаг нужен
---------------
До ``0040`` возможность принадлежала одной процедуре: ``template`` — внешний
ключ, уникальна пара (процедура, ``key``). Решение владельца №5 от 02.10:
одно знание не копируется вручную в десятки строк — запись словаря
существует один раз и привязана к нескольким процедурам. ``0040`` делает
``key`` уникальным самим по себе, а лежащие строки могут иметь один ``key``
у разных процедур.

Что шаг делает
--------------
1. Каждой строке — привязка к её прежней процедуре.
2. Строки с одним ``key`` сводятся в одну запись, только если у них ПОЛНОСТЬЮ
   одинаковы содержание, основание, статус и подписи, а связи с целями не
   пересекаются по цели. Тогда остаётся самая ранняя строка, ей переходят
   привязки, связи с целями и записи журнала снятых подтверждений остальных,
   остальные удаляются. Ничего не теряется: каждое из сведённых утверждений
   было тем же утверждением о своей процедуре.
3. При ЛЮБОМ расхождении строки остаются отдельными записями: самая ранняя
   сохраняет ``key``, остальным ``key`` дополняется кодом процедуры
   (``temporary_relaxation-1-1-3``), и ВСЕ строки этого ``key`` возвращаются
   в черновик со снятой отметкой рецензента. Что из них одна возможность, а
   что разные, код решить не может — решает куратор.

Чего шаг не делает
------------------
* Не сводит молча: свод только при полном совпадении.
* Не угадывает, какое из расходящихся содержаний верно.
* След подтверждения (``confirmed_by``, ``confirmed_at``) не стирает — как
  шаги ``0032``–``0038``.

Числа — сведено, переименовано, в черновик — шаг печатает без содержимого.

Обратный шаг возможен только для словаря, где у каждой записи ровно одна
процедура; иначе он останавливается с объяснением, а не теряет привязки.
"""
from __future__ import annotations

from collections import defaultdict

#: Поля, по которым строки НЕ сравниваются: идентичность, процедура (её как
#: раз и сводят) и служебное время.
NOT_COMPARED = frozenset({"id", "template", "created_at", "updated_at"})

#: Поле ``key`` — ``SlugField(max_length=64)``.
KEY_LENGTH = 64


def _suffix(template) -> str:
    code = (template.canonical_code or "").strip()
    return code.replace(".", "-") if code else template.pk.hex[:8]


def _free_key(base: str, suffix: str, taken: set[str]) -> str:
    candidate = f"{base[: KEY_LENGTH - len(suffix) - 1]}-{suffix}"
    number = 2
    while candidate in taken:
        tail = f"{suffix}-{number}"
        candidate = f"{base[: KEY_LENGTH - len(tail) - 1]}-{tail}"
        number += 1
    taken.add(candidate)
    return candidate


def to_dictionary(capability_model, binding_model, link_model, reset_model) -> dict[str, int]:
    """Привязать строки к их процедурам и свести одинаковые ключи; вернуть числа."""
    compared = [
        f.attname for f in capability_model._meta.concrete_fields if f.name not in NOT_COMPARED
    ]
    rows = list(capability_model.objects.select_related("template").order_by("created_at", "pk"))
    binding_model.objects.bulk_create(
        [binding_model(capability_id=row.pk, template_id=row.template_id) for row in rows]
    )
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[row.key].append(row)
    taken = set(groups)
    counts = {"merged": 0, "renamed": 0, "demoted": 0}
    for key, group in groups.items():
        if len(group) == 1:
            continue
        survivor, others = group[0], group[1:]
        goals = [
            set(link_model.objects.filter(capability_id=row.pk).values_list("goal_id", flat=True))
            for row in group
        ]
        goals_disjoint = sum(len(g) for g in goals) == len(set().union(*goals))
        same = all(
            getattr(row, name) == getattr(survivor, name) for row in others for name in compared
        )
        if same and goals_disjoint:
            for row in others:
                binding_model.objects.filter(capability_id=row.pk).update(capability_id=survivor.pk)
                link_model.objects.filter(capability_id=row.pk).update(capability_id=survivor.pk)
                reset_model.objects.filter(capability_id=row.pk).update(capability_id=survivor.pk)
                row.delete()
            counts["merged"] += len(others)
            continue
        for row in others:
            row.key = _free_key(key, _suffix(row.template), taken)
            row.save(update_fields=["key"])
        counts["renamed"] += len(others)
        counts["demoted"] += capability_model.objects.filter(pk__in=[row.pk for row in group]).update(
            status="system_inference", reviewed_by=None, reviewed_at=None,
        )
    return counts


def from_dictionary(capability_model, binding_model) -> None:
    """Обратно: вернуть каждой записи её единственную процедуру — или остановиться."""
    for capability in capability_model.objects.all():
        template_ids = list(
            binding_model.objects.filter(capability_id=capability.pk).values_list("template_id", flat=True)
        )
        if len(template_ids) != 1:
            raise RuntimeError(
                f"capability {capability.pk} is bound to {len(template_ids)} procedures; "
                "a dictionary entry cannot go back to a single procedure without losing bindings"
            )
        capability.template_id = template_ids[0]
        capability.save(update_fields=["template"])
