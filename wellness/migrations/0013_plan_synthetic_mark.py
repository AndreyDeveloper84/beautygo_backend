"""DRF-2871 — пометка «синтетика» на сохранённом плане.

Решение владельца 08.10.2026 (сквозная проверка Плана на подготовленных
данных): механику пути проверяют на помеченных синтетических знаниях, и это
не должно выглядеть как обоснованность реальных рекомендаций. План, у
которого хоть один шаг стоит на синтетической способности, помечается при
сохранении.

Пометка ставится при создании строки и не меняется никогда — тем же родом
замка, что у знания и услуги в ``services/0052``: триггер ловит и
``QuerySet.update()``. Иначе «снял пометку» превращал бы синтетический план
в настоящий без единого подтверждённого знания под ним.

Существующим строкам колонка даёт ``false``: до этой миграции синтетика не
читалась ни одним писателем плана.
"""
from django.db import migrations, models


TRIGGER = r"""
CREATE FUNCTION wellness_plan_synthetic_mark_is_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.synthetic IS DISTINCT FROM OLD.synthetic THEN
        RAISE EXCEPTION 'synthetic_mark_is_immutable: plan % — the mark is set at creation and never changes',
            OLD.id USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER plan_synthetic_mark_is_immutable
    BEFORE UPDATE ON wellness_plan
    FOR EACH ROW EXECUTE FUNCTION wellness_plan_synthetic_mark_is_immutable();
"""

DROP_TRIGGER = r"""
DROP TRIGGER IF EXISTS plan_synthetic_mark_is_immutable ON wellness_plan;
DROP FUNCTION IF EXISTS wellness_plan_synthetic_mark_is_immutable();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("wellness", "0012_plan_proposed_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="synthetic",
            field=models.BooleanField(
                default=False,
                help_text="План на помеченных синтетических данных; ставится при создании, не меняется",
            ),
        ),
        migrations.RunSQL(TRIGGER, reverse_sql=DROP_TRIGGER),
    ]
