"""Вид доказательства — закрытый список (DRF-2742, решение владельца №4 от 02.10).

Номер 0036 — намеренно через два: 0034 и 0035 заняты миграциями DRF-2726
(право подтверждения и роль рецензента), которые ждут слова владельца в
своих ветках. Зависимость — от 0033, листа ``dev`` на момент этой правки.

Шаги, порядок существенен:

1. поле ``evidence_kind`` получает список выбора (схему базы не меняет);
2. подтверждённые строки с пустым или произвольным видом возвращаются в
   черновик (правило и обоснование — в ``_drf2742_evidence_kind_demote``,
   там же оно проверяется тестом);
3. ограничение: у подтверждённой строки вид — один из восьми.

Обратного действия у шага 2 нет намеренно.
"""
from django.conf import settings
from django.db import migrations, models

from . import _drf2742_evidence_kind_demote as evidence_kind_demote


def _demote(apps, schema_editor):
    counts = evidence_kind_demote.demote(
        apps.get_model("services", "ProcedureCapability"),
        apps.get_model("services", "CapabilityGoalLink"),
    )
    print(
        f"  approved without a known evidence kind -> draft: "
        f"capabilities={counts['capabilities']} goal_links={counts['goal_links']}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0033_drf2726_prohibited_statement"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="capabilitygoallink",
            name="evidence_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("clinical_guideline", "Клиническая рекомендация"),
                    ("systematic_review", "Систематический обзор / мета-анализ"),
                    ("rct", "Рандомизированное контролируемое исследование"),
                    ("manufacturer_ifu", "Официальная инструкция производителя"),
                    ("regulatory_document", "Регуляторный документ"),
                    ("professional_consensus", "Профессиональный консенсус"),
                    ("legal_rule", "Правовая норма"),
                    ("product_policy", "Продуктовая политика"),
                ],
                default="",
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="procedurecapability",
            name="evidence_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("clinical_guideline", "Клиническая рекомендация"),
                    ("systematic_review", "Систематический обзор / мета-анализ"),
                    ("rct", "Рандомизированное контролируемое исследование"),
                    ("manufacturer_ifu", "Официальная инструкция производителя"),
                    ("regulatory_document", "Регуляторный документ"),
                    ("professional_consensus", "Профессиональный консенсус"),
                    ("legal_rule", "Правовая норма"),
                    ("product_policy", "Продуктовая политика"),
                ],
                default="",
                max_length=64,
            ),
        ),
        migrations.RunPython(_demote, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    (
                        "evidence_kind__in",
                        [
                            "clinical_guideline",
                            "systematic_review",
                            "rct",
                            "manufacturer_ifu",
                            "regulatory_document",
                            "professional_consensus",
                            "legal_rule",
                            "product_policy",
                        ],
                    ),
                    _connector="OR",
                ),
                name="capabilitygoallink_approved_evidence_kind_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    (
                        "evidence_kind__in",
                        [
                            "clinical_guideline",
                            "systematic_review",
                            "rct",
                            "manufacturer_ifu",
                            "regulatory_document",
                            "professional_consensus",
                            "legal_rule",
                            "product_policy",
                        ],
                    ),
                    _connector="OR",
                ),
                name="procedurecapability_approved_evidence_kind_known",
            ),
        ),
    ]
