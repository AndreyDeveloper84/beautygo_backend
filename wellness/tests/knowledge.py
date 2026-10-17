"""Подтверждённое знание для узлов Plan Engine (DRF-2871).

Команда сохранения плана принимает шаг только на способности, о которой
ПОДТВЕРЖДЕНО, что она помогает цели человека. Узлам, которые сохраняют план
ради другого предмета (идемпотентность, шаги, записи), это знание нужно как
данность — оно заводится здесь, одним местом.

Способность создаётся помощником узлов сборки: он единственный знает, как
сегодня устроена запись о способности (схема меняется с общим словарём).
"""
from __future__ import annotations

from services.models import GoalOption, ProcedureCapability, ServiceCategory, ServiceTemplate
from users.models import User

#: Способность, на которой стоят шаги в узлах хранения, шагов и записей.
CAPABILITY = "cap:relaxation-massage"
GOAL_KEY = "relax"


def confirm_capability_for_goal(key: str = CAPABILITY, goal_key: str = GOAL_KEY) -> None:
    """Подтверждённая способность с подтверждённой связью на цель; повторный
    вызов ничего не дублирует."""
    from wellness.tests.test_plan_compose_2871 import _capability

    if ProcedureCapability.objects.filter(key=key).exists():
        return
    option, _ = GoalOption.objects.get_or_create(key=goal_key, defaults={"label": goal_key})
    category, _ = ServiceCategory.objects.get_or_create(
        slug="plan-knowledge-fixture", defaults={"name": "Plan knowledge fixture"},
    )
    template, _ = ServiceTemplate.objects.get_or_create(category=category, name="Plan knowledge fixture")
    curator = User.objects.filter(username="plan_knowledge_curator").first() or User.objects.create_user(
        username="plan_knowledge_curator", password="x", role="client", phone="+79990002871",
    )
    _capability(template, key, curator, option)
