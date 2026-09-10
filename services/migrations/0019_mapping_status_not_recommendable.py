"""Четвёртое состояние связи и ужесточение происхождения.

В одной миграции живут ДВА разных инварианта. Это сделано намеренно —
один разговор с базой вместо двух, — но при откате их легко перепутать,
поэтому они названы порознь здесь, а не только в описании PR.

**Первый — третий исход разбора (§93).** `not_recommendable` рядом с
`unmapped`: «связи не будет, и это решено» против «связи ещё нет».
Ширина колонки поднята с 16 до 24, потому что значение длиннее — имя
выбрано по смыслу, ширина подогнана под имя, а не наоборот. Своё
ограничение происхождения: отказ — решение, и без автора он через месяц
читается как умолчание.

**Второй — ужесточение, которое чинит расхождение.** Докстринг
`SalonService.mapping_confirmed_by` со дня своего появления называет
поля «кто» и «правило» взаимоисключающими и ссылается на §76. Оба
ограничения происхождения при этом написаны через `OR` и строку с
обоими заполненными пропускали. То есть решение владельца было записано
и не исполнялось, а выглядело исполненным.

Замер перед ужесточением — **главное окно, 10.09.2026, `dev-web-1`**
(своего доступа к боевому контуру у автора миграции не было, число
чужое и датировано намеренно)::

    SalonService всего                265
    оба поля заполнены (by И rule)      0
    только by                           0
    только rule                         0
    verified                            0

**Ни одна существующая строка ужесточение не нарушает**, поэтому
бэкфилла нет. Если эту миграцию когда-нибудь откатят и накатят заново
на других данных — проверьте это число снова, а не поверьте ему: здесь
записано, что проверялось, а не что предполагалось.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0018_salonservice_health_check_tristate"),
        ("tenants", "0004_tenant_address_city"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="salonservice",
            name="mapping_status",
            field=models.CharField(
                choices=[
                    ("unmapped", "Unmapped"),
                    ("review_required", "Review required"),
                    ("verified", "Verified"),
                    ("not_recommendable", "Не подлежит рекомендациям"),
                ],
                default="unmapped",
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="salonservice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("mapping_status", "not_recommendable"), _negated=True),
                    models.Q(
                        ("mapping_confirmed_at__isnull", False),
                        models.Q(("mapping_source_ref", ""), _negated=True),
                        models.Q(
                            ("mapping_confirmed_by__isnull", False),
                            models.Q(("mapping_confirmed_rule", ""), _negated=True),
                            _connector="OR",
                        ),
                    ),
                    _connector="OR",
                ),
                name="salonservice_not_recommendable_requires_provenance",
            ),
        ),
        migrations.AddConstraint(
            model_name="salonservice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("mapping_confirmed_by__isnull", False),
                    models.Q(("mapping_confirmed_rule", ""), _negated=True),
                    _negated=True,
                ),
                name="salonservice_provenance_is_who_xor_rule",
            ),
        ),
    ]
