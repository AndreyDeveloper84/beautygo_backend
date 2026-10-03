"""Проверка рецензентом — отдельный факт от подтверждения (DRF-2726, блок A).

Решение владельца 02.10: три роли — **куратор** собирает, **предметный
рецензент** проверяет профессиональные, физиологические и медицинские
утверждения в пределах своей компетенции, **владелец продукта** держит
продуктовые границы. «``confirmed_by`` без проверки роли, компетенции и типа
утверждения — недостаточно»; медицинские и физиологические утверждения —
«только назначенным рецензентом подходящей квалификации».

Как это устроено
----------------
* у утверждения есть **тип** (``ClaimEvidence.claim_type``);
* **проверка** — свои поля (``reviewed_by``, ``reviewed_at``), не
  ``confirmed_by``: «рецензент проверил факт» и «куратор утвердил
  использование» — два решения, и у каждого свой автор;
* **компетенция** — строка :class:`~services.models.ClaimReviewer`: человек,
  тип утверждения, необязательная область (категория каталога). Отметить
  «проверено» может только тот, у кого есть такая действующая строка под тип
  и область этого утверждения. Право администратора компетенцией не является:
  суперпользователь без назначения проверить не может.

Кто решает, что рецензент не нужен
----------------------------------
Тип объявляет тот, кто заводит строку, — то есть тот, кого тип ограничивает.
Поэтому подтвердить утверждение типа, НЕ требующего рецензента (``product``),
может только держатель отдельного права
``services.approve_claim_without_reviewer`` — третья роль блока A, «владелец
продукта (продуктовые границы)». Обычный куратор с правом подтверждения
подтверждает только проверенное рецензентом; переименовать медицинское
утверждение в продуктовое и подтвердить в одиночку он не может.

Пределы, названные честно
-------------------------
* **Самоназначение не запрещено.** Тот, кто вправе вести таблицу назначений
  (по умолчанию — суперпользователь), может назначить рецензентом себя:
  владелец может оказаться единственным рецензентом в своей области.
  Компетенцию удостоверяет назначающий; след — в журнале админки.
* **Держатель продуктовых границ может объявить продуктовым что угодно.** Это
  его решение, и оно подписано его именем (``confirmed_by``); код не судит,
  медицинское ли утверждение по смыслу.
* Правка названия цели (``GoalOption``) проверку связей не снимает.

Что здесь умолчание, а не решение владельца
-------------------------------------------
Имён в коде нет — рецензентов назначает владелец в админке. Но три выбора
сделаны по умолчанию и названы в теле PR:

1. список типов утверждения (``ClaimEvidence.ClaimType``);
2. какие типы требуют рецензента — все, кроме ``product``
   (``ClaimEvidence.REVIEW_REQUIRED_CLAIM_TYPES``);
3. один человек может и проверить, и подтвердить одну строку — запрета нет;
4. подтверждение без рецензента — отдельное право, у суперпользователя есть
   всегда.
"""

from __future__ import annotations

from django.db.models import Q

from services.models import ClaimEvidence, ClaimReviewer, ServiceTemplate

UNCLASSIFIED = ClaimEvidence.ClaimType.UNCLASSIFIED.value

#: Право подтверждать утверждение без рецензента — одно на обе таблицы знания.
WITHOUT_REVIEWER_PERMISSION = "services.approve_claim_without_reviewer"


def may_approve_without_reviewer(user) -> bool:
    """Вправе ли человек подтвердить утверждение, которому рецензент не нужен."""
    return user is not None and user.has_perm(WITHOUT_REVIEWER_PERMISSION)


def requires_review(claim_type: str) -> bool:
    """Нужна ли утверждению этого типа проверка рецензентом."""
    return claim_type in ClaimEvidence.REVIEW_REQUIRED_CLAIM_TYPES


def may_review(user, *, claim_type: str, template: ServiceTemplate | None) -> bool:
    """Вправе ли человек проверить утверждение этого типа об этой процедуре.

    Нужна действующая строка назначения под тип; область назначения — пусто
    (любая) либо категория процедуры или её родительская категория. Нет
    пользователя, учётка не действует, тип не требует рецензента — нет.
    """
    if user is None or not getattr(user, "is_active", False) or not getattr(user, "pk", None):
        return False
    if not requires_review(claim_type):
        return False
    appointments = ClaimReviewer.objects.filter(user=user, claim_type=claim_type, is_active=True)
    scope = Q(category__isnull=True)
    if template is not None and template.category_id is not None:
        categories = [template.category_id]
        if template.category.parent_id is not None:
            categories.append(template.category.parent_id)
        scope |= Q(category_id__in=categories)
    return appointments.filter(scope).exists()


def may_review_scope(user, *, claim_type: str, templates, categories) -> bool:
    """Вправе ли человек проверить утверждение с ОБЛАСТЬЮ применения (DRF-2741).

    У противопоказания область — несколько процедур и категорий. Рецензент
    должен быть вправе проверять по КАЖДОЙ из них: назначение без области
    покрывает всё; назначение на область — её процедуры и подкатегории. Пустая
    область — только рецензент без ограничения области.
    """
    if user is None or not getattr(user, "is_active", False) or not getattr(user, "pk", None):
        return False
    if not requires_review(claim_type):
        return False
    appointments = ClaimReviewer.objects.filter(user=user, claim_type=claim_type, is_active=True)
    if appointments.filter(category__isnull=True).exists():
        return True
    covered = set(appointments.values_list("category_id", flat=True))
    areas = [(t.category_id, t.category.parent_id) for t in templates]
    areas += [(c.pk, c.parent_id) for c in categories]
    return bool(areas) and all(own in covered or parent in covered for own, parent in areas)


__all__ = [
    "UNCLASSIFIED",
    "WITHOUT_REVIEWER_PERMISSION",
    "may_approve_without_reviewer",
    "may_review",
    "may_review_scope",
    "requires_review",
]
