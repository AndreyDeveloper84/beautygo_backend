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

Что здесь умолчание, а не решение владельца
-------------------------------------------
Имён в коде нет — рецензентов назначает владелец в админке. Но три выбора
сделаны по умолчанию и названы в теле PR:

1. список типов утверждения (``ClaimEvidence.ClaimType``);
2. какие типы требуют рецензента — все, кроме ``product``
   (``ClaimEvidence.REVIEW_REQUIRED_CLAIM_TYPES``);
3. один человек может и проверить, и подтвердить одну строку — запрета нет.
"""

from __future__ import annotations

from django.db.models import Q

from services.models import ClaimEvidence, ClaimReviewer, ServiceTemplate

UNCLASSIFIED = ClaimEvidence.ClaimType.UNCLASSIFIED.value


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


__all__ = ["UNCLASSIFIED", "may_review", "requires_review"]
