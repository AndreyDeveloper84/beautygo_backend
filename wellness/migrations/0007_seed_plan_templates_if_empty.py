"""Заполнить пустоту: шаблоны Plan Lite §51 для целей, у которых нет НИ ОДНОЙ версии (DRF-2229).

Plan Lite на стенде выключен, потому что наличие шаблонов §51 не
гарантировано: их кладёт только ручная команда ``seed_plan_templates``.
Эта миграция делает наличие гарантированным — и ничего больше:

* цель, у которой в ``PlanTemplate`` нет ни одной строки, получает версию 1
  из ``wellness.plan_lite_templates.PLAN_TEMPLATES_SEED`` (активную);
* цель, у которой строки есть — активная (в т.ч. правленная в админке) или
  только снятые, — **не трогается**: ни новой версии, ни снятия. Снятые все
  версии — это решение того, кто их снял в админке, а не пустота;
* повтор — без изменений (идемпотентна по построению);
* обратная — ничего не делает: удалять данные, на которые могли сослаться
  планы (``PersonalPlan.source``), нельзя.

Вопрос «источник истины — админка или код» миграция не решает: она не
сравнивает содержимое и не выравнивает его. Это делает ``seed_plan_templates``
— и он по-прежнему кладёт версию кода ПОВЕРХ отличающейся активной (в т.ч.
админской); поведение команды этой миграцией не меняется (названо в PR).

Данные берутся из модуля, а не копией сюда: у таблицы один источник в коде.
Запись — через историческую модель (``apps.get_model``).
"""

from __future__ import annotations

from django.db import migrations


def seed_missing_goals(apps, schema_editor) -> None:
    from wellness.plan_lite_templates import PLAN_TEMPLATES_SEED

    PlanTemplate = apps.get_model("wellness", "PlanTemplate")
    known = set(PlanTemplate.objects.values_list("goal_key", flat=True).distinct())
    for row in PLAN_TEMPLATES_SEED:
        if row["goal_key"] in known:
            continue
        PlanTemplate.objects.create(
            goal_key=row["goal_key"],
            actions=row["actions"],
            why_text=row["why_text"],
            nutrition_goal_hint=list(row.get("nutrition_goal_hint") or []),
            version=1,
            is_active=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("wellness", "0006_plantemplate_nutrition_goal_hint"),
    ]

    operations = [
        migrations.RunPython(seed_missing_goals, migrations.RunPython.noop),
    ]
