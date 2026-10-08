"""Происхождение ответа салона «нужна ли проверка перед услугой» (DRF-2877, S2).

Решение владельца 07.10: неподтверждённое «проверка не нужна» — это
«неизвестно», и смотреть надо происхождение конкретного поля, а не способ
создания строки услуги. До этой миграции у ответа салона не было ни автора,
ни даты: значение, скопированное из канона сидером, и значение, выставленное
без автора, выглядели в базе ответом салона.

Только схема. **Существующим строкам миграция ничего не объявляет**: все они
получают «не подтверждён» умолчанием колонки — ни одно лежащее «нужна» или
«не нужна» человек не подтверждал, и подставлять подтверждение миграция не
вправе. Вердикт гейта записи не меняется.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0050_drf2743_capability_dictionary"),
        ("tenants", "0011_body_care_7a2_medical_license"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="salonservice",
            name="health_check_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salonservice",
            name="health_check_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="salonservice",
            name="health_check_confirmed_rule",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="salonservice",
            name="health_check_origin",
            field=models.CharField(
                choices=[("unset", "Не подтверждён"), ("confirmed", "Подтверждён")],
                default="unset",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="salonservice",
            name="health_check_rule_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="salonservice",
            name="health_check_source_ref",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddConstraint(
            model_name="salonservice",
            constraint=models.CheckConstraint(
                condition=models.Q(("health_check_origin__in", ["unset", "confirmed"])),
                name="salonservice_health_check_origin_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="salonservice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("health_check_origin", "confirmed"), _negated=True),
                    models.Q(
                        ("requires_health_check__isnull", False),
                        ("health_check_confirmed_at__isnull", False),
                        models.Q(("health_check_source_ref", ""), _negated=True),
                        models.Q(
                            models.Q(
                                ("health_check_confirmed_by__isnull", False),
                                ("health_check_confirmed_rule", ""),
                                ("health_check_rule_version", ""),
                            ),
                            models.Q(
                                ("health_check_confirmed_by__isnull", True),
                                models.Q(
                                    ("health_check_confirmed_rule", ""), _negated=True
                                ),
                                models.Q(
                                    ("health_check_rule_version", ""), _negated=True
                                ),
                            ),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="salonservice_health_check_confirmed_requires_provenance",
            ),
        ),
        migrations.AddConstraint(
            model_name="salonservice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("health_check_origin", "unset"), _negated=True),
                    models.Q(
                        ("health_check_confirmed_by__isnull", True),
                        ("health_check_confirmed_rule", ""),
                        ("health_check_rule_version", ""),
                        ("health_check_confirmed_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="salonservice_health_check_unset_carries_no_confirmation",
            ),
        ),
    ]
