"""Область классификации канона: подлежит ли Body Care (решение владельца 07.10).

Схема и один шаг данных. До этой миграции «вне Body Care» значило
«семейства нет» — отсутствие читалось как классификация. Теперь область —
отдельное поле: ``NULL`` — неизвестно, ``not_body_care`` — подтверждено.

Шаг данных стоит между полями и ограничениями: канон, у которого семейство
уже назначено, получает ``body_care`` — иначе ограничение «семейство
требует области» не встало бы на лежащих строках. Автор — правило с
версией: семейство уже утверждает, что канон подлежит Body Care, и нового
решения здесь нет. Каноны без семейства шаг не трогает: их область
неизвестна, и ставит её человек или правило раздела отдельным шагом.

Поведение читателей эта миграция не меняет — оно под флагом
``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED``.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone

#: Литералы продублированы намеренно: миграция не зависит от кода
#: приложения, который завтра переименуют.
RULE = "family_implies_body_care"
RULE_VERSION = "1"
SOURCE_REF = "services/0048: семейство Body Care назначено каноном до появления поля области"


def scope_canons_with_a_family(apps, schema_editor):
    ServiceTemplate = apps.get_model("services", "ServiceTemplate")
    touched = ServiceTemplate.objects.filter(
        service_family__isnull=False, body_care_scope__isnull=True
    ).update(
        body_care_scope="body_care",
        scope_confirmed_rule=RULE,
        scope_rule_version=RULE_VERSION,
        scope_confirmed_at=timezone.now(),
        scope_source_ref=SOURCE_REF,
    )
    # Следом идёт ALTER той же таблицы: отложенные проверки не должны
    # остаться висеть до конца транзакции.
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    print(f"  0048 body_care_scope: канонов с семейством получили область {touched}")


def unscope(apps, schema_editor):
    """Снять только то, что поставил этот шаг: поля ниже всё равно удаляются."""
    ServiceTemplate = apps.get_model("services", "ServiceTemplate")
    ServiceTemplate.objects.filter(scope_confirmed_rule=RULE).update(
        body_care_scope=None,
        scope_confirmed_rule="",
        scope_rule_version="",
        scope_confirmed_at=None,
        scope_source_ref="",
    )
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0047_drf2852_peelings_legal_review"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="servicetemplate",
            name="body_care_scope",
            field=models.CharField(
                blank=True,
                choices=[
                    ("body_care", "Подлежит Body Care"),
                    ("not_body_care", "Вне Body Care"),
                ],
                help_text="Подлежит ли Body Care; пусто — классификация неизвестна, читается как «не допущено»",
                max_length=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="servicetemplate",
            name="scope_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="servicetemplate",
            name="scope_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="servicetemplate",
            name="scope_confirmed_rule",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="servicetemplate",
            name="scope_rule_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="servicetemplate",
            name="scope_source_ref",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.RunPython(scope_canons_with_a_family, unscope),
        migrations.AddConstraint(
            model_name="servicetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("body_care_scope__isnull", True),
                    ("body_care_scope__in", ["body_care", "not_body_care"]),
                    _connector="OR",
                ),
                name="servicetemplate_body_care_scope_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("service_family__isnull", True),
                    models.Q(
                        ("body_care_scope__isnull", False),
                        ("body_care_scope", "body_care"),
                    ),
                    _connector="OR",
                ),
                name="servicetemplate_family_requires_body_care_scope",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicetemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("body_care_scope__isnull", True),
                    models.Q(
                        ("scope_confirmed_at__isnull", False),
                        models.Q(("scope_source_ref", ""), _negated=True),
                        models.Q(
                            models.Q(
                                ("scope_confirmed_by__isnull", False),
                                ("scope_confirmed_rule", ""),
                                ("scope_rule_version", ""),
                            ),
                            models.Q(
                                ("scope_confirmed_by__isnull", True),
                                models.Q(("scope_confirmed_rule", ""), _negated=True),
                                models.Q(("scope_rule_version", ""), _negated=True),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="servicetemplate_body_care_scope_requires_provenance",
            ),
        ),
    ]
