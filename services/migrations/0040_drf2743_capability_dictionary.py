"""Общий словарь возможностей (DRF-2743, решение владельца №5 от 02.10).

Возможность была строкой одной процедуры (``template`` — внешний ключ,
уникальна пара (процедура, ``key``)). Теперь это запись словаря: ``key``
уникален сам по себе, процедур у записи сколько угодно — через таблицу
привязок ``CapabilityTemplate``, которая, как прежний ключ, запрещает удалить
процедуру с привязанным знанием.

Шаги, порядок существенен:

1. таблица привязок;
2. прежний ``template`` становится необязательным и теряет обратное имя —
   чтобы новое поле могло занять ``capabilities``;
3. связь «многие ко многим» ``templates``;
4. перенос данных (правило и обоснование — в ``_drf2743_dictionary``, там же
   оно проверяется тестом): каждой строке — её процедура; одинаковые ключи
   сводятся только при полном совпадении, иначе переименовываются и уходят в
   черновик;
5. снятие пары (процедура, ``key``) и прежнего поля;
6. ``key`` уникален.

Обратный путь есть, пока у каждой записи ровно одна процедура; иначе шаг 4
останавливается с объяснением.
"""
import django.db.models.deletion
from django.db import migrations, models

from . import _drf2743_dictionary as dictionary


def _flush_deferred_checks(schema_editor) -> None:
    """Проверить отложенные внешние ключи сейчас, а не в конце транзакции.

    Миграция идёт одной транзакцией. Шаг переноса меняет и удаляет строки, а
    проверки внешних ключей в PostgreSQL отложены до конца транзакции; пока они
    висят, следующий ``ALTER TABLE`` той же таблицы падает («pending trigger
    events») — то есть ``migrate`` на стенде встал бы на первой же лежащей строке.
    """
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _forward(apps, schema_editor):
    counts = dictionary.to_dictionary(
        apps.get_model("services", "ProcedureCapability"),
        apps.get_model("services", "CapabilityTemplate"),
        apps.get_model("services", "CapabilityGoalLink"),
        apps.get_model("services", "ClaimApprovalReset"),
    )
    print(
        f"  capabilities -> dictionary: merged={counts['merged']} "
        f"renamed={counts['renamed']} returned to draft={counts['demoted']} "
        f"goal links returned to draft={counts['links_demoted']}"
    )
    _flush_deferred_checks(schema_editor)


def _backward(apps, schema_editor):
    dictionary.from_dictionary(
        apps.get_model("services", "ProcedureCapability"),
        apps.get_model("services", "CapabilityTemplate"),
    )
    _flush_deferred_checks(schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0039_drf2741_contraindication_rules"),
    ]

    operations = [
        migrations.CreateModel(
            name="CapabilityTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "capability",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="template_bindings",
                        to="services.procedurecapability",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="capability_bindings",
                        to="services.servicetemplate",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("capability", "template"),
                        name="capabilitytemplate_capability_template_uniq",
                    )
                ],
            },
        ),
        migrations.AlterField(
            model_name="procedurecapability",
            name="template",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="services.servicetemplate",
            ),
        ),
        migrations.AddField(
            model_name="procedurecapability",
            name="templates",
            field=models.ManyToManyField(
                related_name="capabilities",
                through="services.CapabilityTemplate",
                to="services.servicetemplate",
            ),
        ),
        migrations.RunPython(_forward, _backward),
        migrations.RemoveConstraint(
            model_name="procedurecapability",
            name="procedurecapability_template_key_uniq",
        ),
        migrations.RemoveField(
            model_name="procedurecapability",
            name="template",
        ),
        migrations.AlterField(
            model_name="procedurecapability",
            name="key",
            field=models.SlugField(max_length=64, unique=True),
        ),
        migrations.AlterModelOptions(
            name="procedurecapability",
            options={
                "ordering": ["key"],
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
    ]
