"""Проверка рецензентом — отдельно от подтверждения (DRF-2726, решение владельца 02.10, блок A).

Шаги, порядок существенен:

1. таблица назначений рецензентов (``ClaimReviewer``);
2. колонки у обеих таблиц знания: тип утверждения (по умолчанию
   ``unclassified``) и отметка проверки (``reviewed_by``, ``reviewed_at``);
3. ВСЕ подтверждённые строки возвращаются в черновик (правило и обоснование —
   в ``_drf2726_review_demote``, там же оно проверяется тестом): у строки без
   типа нет способа узнать, нужен ли ей рецензент;
4. ограничения: подтверждённая строка имеет тип, а тип, требующий рецензента,
   — отметку проверки.

Обратного действия у шага 3 нет намеренно.
"""
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

from . import _drf2726_review_demote as review_demote


def _demote(apps, schema_editor):
    counts = review_demote.demote(
        apps.get_model("services", "ProcedureCapability"),
        apps.get_model("services", "CapabilityGoalLink"),
    )
    print(
        f"  approved without a claim type -> draft: "
        f"capabilities={counts['capabilities']} goal_links={counts['goal_links']}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0034_drf2726_approval_right"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ClaimReviewer",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "claim_type",
                    models.CharField(
                        choices=[
                            ("unclassified", "Не указан"),
                            ("product", "Продуктовое"),
                            ("professional", "Профессиональное"),
                            ("physiological", "Физиологическое"),
                            ("medical", "Медицинское"),
                        ],
                        max_length=16,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["user", "claim_type"],
            },
        ),
        migrations.AddField(
            model_name="capabilitygoallink",
            name="claim_type",
            field=models.CharField(
                choices=[
                    ("unclassified", "Не указан"),
                    ("product", "Продуктовое"),
                    ("professional", "Профессиональное"),
                    ("physiological", "Физиологическое"),
                    ("medical", "Медицинское"),
                ],
                default="unclassified",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="capabilitygoallink",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="capabilitygoallink",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="procedurecapability",
            name="claim_type",
            field=models.CharField(
                choices=[
                    ("unclassified", "Не указан"),
                    ("product", "Продуктовое"),
                    ("professional", "Профессиональное"),
                    ("physiological", "Физиологическое"),
                    ("medical", "Медицинское"),
                ],
                default="unclassified",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="procedurecapability",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="procedurecapability",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(_demote, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    models.Q(("claim_type", "unclassified"), _negated=True),
                    _connector="OR",
                ),
                name="capabilitygoallink_approved_has_claim_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    models.Q(
                        (
                            "claim_type__in",
                            ["professional", "physiological", "medical"],
                        ),
                        _negated=True,
                    ),
                    models.Q(
                        ("reviewed_by__isnull", False), ("reviewed_at__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="capabilitygoallink_approved_review_when_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("reviewed_by__isnull", True), ("reviewed_at__isnull", True)
                    ),
                    models.Q(
                        ("reviewed_by__isnull", False), ("reviewed_at__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="capabilitygoallink_review_is_whole",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    models.Q(("claim_type", "unclassified"), _negated=True),
                    _connector="OR",
                ),
                name="procedurecapability_approved_has_claim_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    models.Q(
                        (
                            "claim_type__in",
                            ["professional", "physiological", "medical"],
                        ),
                        _negated=True,
                    ),
                    models.Q(
                        ("reviewed_by__isnull", False), ("reviewed_at__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="procedurecapability_approved_review_when_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("reviewed_by__isnull", True), ("reviewed_at__isnull", True)
                    ),
                    models.Q(
                        ("reviewed_by__isnull", False), ("reviewed_at__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="procedurecapability_review_is_whole",
            ),
        ),
        migrations.AddField(
            model_name="claimreviewer",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="services.servicecategory",
            ),
        ),
        migrations.AddField(
            model_name="claimreviewer",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="claim_reviewer_appointments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="claimreviewer",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("claim_type__in", ["professional", "physiological", "medical"])
                ),
                name="claimreviewer_type_is_reviewable",
            ),
        ),
        migrations.AddConstraint(
            model_name="claimreviewer",
            constraint=models.UniqueConstraint(
                condition=models.Q(("category__isnull", False)),
                fields=("user", "claim_type", "category"),
                name="claimreviewer_user_type_category_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="claimreviewer",
            constraint=models.UniqueConstraint(
                condition=models.Q(("category__isnull", True)),
                fields=("user", "claim_type"),
                name="claimreviewer_user_type_any_category_uniq",
            ),
        ),
        # Право подтверждать утверждение без рецензента (продуктовые границы).
        migrations.AlterModelOptions(
            name="procedurecapability",
            options={
                "ordering": ["template", "key"],
                "permissions": [
                    (
                        "approve_procedurecapability",
                        "Может подтверждать возможности процедур",
                    ),
                    (
                        "approve_claim_without_reviewer",
                        "Может подтверждать утверждения без рецензента (продуктовые границы)",
                    ),
                ],
            },
        ),
        # Журнал снятых подтверждений.
        migrations.CreateModel(
            name="ClaimApprovalReset",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("claim_edited", "Изменено само утверждение"),
                            (
                                "capability_edited",
                                "Изменена возможность, о которой связь",
                            ),
                            ("procedure_changed", "Изменены значимые данные процедуры"),
                            ("category_moved", "Категория процедуры перенесена"),
                            ("goal_changed", "Изменена цель, о которой связь"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "claim_kind",
                    models.CharField(
                        choices=[
                            ("capability", "Возможность"),
                            ("goal_link", "Связь с целью"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "claim_label",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("changes", models.JSONField(default=list)),
                ("was_approved", models.BooleanField(default=False)),
                ("had_review", models.BooleanField(default=False)),
                (
                    "capability",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approval_resets",
                        to="services.procedurecapability",
                    ),
                ),
                (
                    "goal_link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approval_resets",
                        to="services.capabilitygoallink",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("capability__isnull", True),
                            ("goal_link__isnull", True),
                            _connector="OR",
                        ),
                        name="claimapprovalreset_at_most_one_claim",
                    )
                ],
            },
        ),
    ]
