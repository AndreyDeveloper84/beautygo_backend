"""Перенос возможностей в общий словарь — общее для миграции и теста (DRF-2743).

Модуль лежит в пакете миграций намеренно, как ``_drf2742_evidence_kind_demote``:
логика принадлежит миграции ``0050``, имя на ``_`` загрузчик Django
миграцией не считает.

Зачем шаг нужен
---------------
До ``0050`` возможность принадлежала одной процедуре: ``template`` — внешний
ключ, уникальна пара (процедура, ``key``). Решение владельца №5 от 02.10:
одно знание не копируется вручную в десятки строк — запись словаря
существует один раз и привязана к нескольким процедурам. ``0050`` делает
``key`` уникальным самим по себе, а лежащие строки могут иметь один ``key``
у разных процедур.

Что шаг делает
--------------
1. Каждой строке — привязка к её прежней процедуре.
2. Строки с одним ``key`` сводятся в одну запись, только если у них ПОЛНОСТЬЮ
   одинаковы содержание, основание, статус и подписи, И одинаковы связи с
   целями: те же цели с тем же содержанием связи. Тогда остаётся самая ранняя
   строка с её связями, ей переходят привязки и записи журнала остальных;
   связи остальных — те же утверждения — удаляются, их записи журнала
   переходят к связям выжившей. Ничего не теряется и ничего не расширяется:
   каждое сведённое утверждение — и возможность, и каждая её связь — уже было
   тем же утверждением о своей процедуре. Связь, одобренная про одну
   процедуру, к другой не переезжает.
3. При ЛЮБОМ расхождении строки остаются отдельными записями: самая ранняя
   сохраняет ``key``, остальным ``key`` дополняется кодом процедуры
   (``temporary_relaxation-1-1-3``), и ВСЕ строки этого ``key`` возвращаются
   в черновик со снятой отметкой рецензента, а их подтверждённые и
   проверенные связи с целями — следом, как при правке возможности в
   админке. Каждая такая строка пишется в журнал снятых подтверждений, чтобы
   попасть в очередь куратора. Что из них одна возможность, а что разные,
   код решить не может — решает куратор.

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

#: Поля связи с целью, по которым связи НЕ сравниваются.
LINK_NOT_COMPARED = frozenset({"id", "capability", "created_at", "updated_at"})

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


def _links(link_model, capability_id, compared) -> dict:
    """Связи строки: ``{goal_id: (связь, сравниваемые значения)}``."""
    return {
        link.goal_id: (link, tuple(getattr(link, name) for name in compared))
        for link in link_model.objects.filter(capability_id=capability_id)
    }


def _has_something_to_lose(row) -> bool:
    return row.status == "approved" or row.reviewed_by_id is not None


def to_dictionary(capability_model, binding_model, link_model, reset_model) -> dict[str, int]:
    """Привязать строки к их процедурам и свести одинаковые ключи; вернуть числа."""
    compared = [
        f.attname for f in capability_model._meta.concrete_fields if f.name not in NOT_COMPARED
    ]
    link_compared = [
        f.attname for f in link_model._meta.concrete_fields if f.name not in LINK_NOT_COMPARED
    ]
    rows = list(capability_model.objects.select_related("template").order_by("created_at", "pk"))
    binding_model.objects.bulk_create(
        [binding_model(capability_id=row.pk, template_id=row.template_id) for row in rows]
    )
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[row.key].append(row)
    taken = set(groups)
    counts = {"merged": 0, "renamed": 0, "demoted": 0, "links_demoted": 0}
    journal = []
    for key, group in groups.items():
        if len(group) == 1:
            continue
        survivor, others = group[0], group[1:]
        links = [_links(link_model, row.pk, link_compared) for row in group]
        same_links = all(
            {goal: values for goal, (_, values) in row_links.items()}
            == {goal: values for goal, (_, values) in links[0].items()}
            for row_links in links[1:]
        )
        same = all(
            getattr(row, name) == getattr(survivor, name) for row in others for name in compared
        )
        if same and same_links:
            for row, row_links in zip(others, links[1:]):
                binding_model.objects.filter(capability_id=row.pk).update(capability_id=survivor.pk)
                reset_model.objects.filter(capability_id=row.pk).update(capability_id=survivor.pk)
                for goal, (link, _) in row_links.items():
                    reset_model.objects.filter(goal_link_id=link.pk).update(goal_link_id=links[0][goal][0].pk)
                row.delete()  # его связи — те же утверждения, что у выжившей, — уходят каскадом
            counts["merged"] += len(others)
            continue
        old_keys = {row.pk: row.key for row in group}
        for row in others:
            row.key = _free_key(key, _suffix(row.template), taken)
            row.save(update_fields=["key"])
        counts["renamed"] += len(others)
        siblings = ", ".join(row.key for row in group)
        for row, row_links in zip(group, links):
            label = f"{row.template.name} · {row.key}"[:300]
            # Причина — не правка этой строки, а соседи под тем же ключом с
            # другим содержанием или другими связями: куратор решает, одна это
            # возможность или разные.
            change = [
                {"field": "key", "old": old_keys[row.pk], "new": row.key},
                {"field": "одинаковый key, разное содержание (DRF-2743)", "old": key, "new": siblings},
            ]
            if _has_something_to_lose(row):
                journal.append(reset_model(
                    reason="claim_edited", changes=change, claim_kind="capability", claim_label=label,
                    capability_id=row.pk, was_approved=row.status == "approved",
                    had_review=row.reviewed_by_id is not None,
                ))
                counts["demoted"] += 1
            for link, _ in row_links.values():
                if _has_something_to_lose(link):
                    journal.append(reset_model(
                        reason="capability_edited", changes=change, claim_kind="goal_link",
                        claim_label=f"{label} → {link.goal.key}"[:300], goal_link_id=link.pk,
                        was_approved=link.status == "approved", had_review=link.reviewed_by_id is not None,
                    ))
                    counts["links_demoted"] += 1
        capability_model.objects.filter(pk__in=[row.pk for row in group]).update(
            status="system_inference", reviewed_by=None, reviewed_at=None,
        )
        link_model.objects.filter(capability_id__in=[row.pk for row in group]).update(
            status="system_inference", reviewed_by=None, reviewed_at=None,
        )
    reset_model.objects.bulk_create(journal)
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
