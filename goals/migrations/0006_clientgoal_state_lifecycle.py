# DRF-1660 — ClientGoal.is_active (bool) -> ClientGoal.state (§97 OD-GOAL-B).
#
# Написана руками, а не автогенератором: тот удалял ``is_active`` ДО
# появления ``state``, то есть терял, какие строки были закрыты. Порядок
# здесь — единственный, при котором данные не теряются:
#
#   1. добавить ``state`` (default active) и ``state_changed_at``;
#   2. перенести: is_active=False -> LEGACY_INACTIVE_REASON_UNKNOWN.
#      НЕ в ACHIEVED и НЕ в ARCHIVED — их семантика неизвестна, и §97
#      прямо запрещает угадывать до управляемой сверки. is_active=True
#      -> ACTIVE (умолчание поля), state_changed_at у всех NULL: момент
#      закрытия старых строк неизвестен так же, как причина;
#   3. поставить частичное ограничение и индекс на ``state``;
#   4. только теперь снять ``is_active``.
#
# Старое ограничение снимается ДО переноса (см. комментарий у операции):
# это нужно обратному ходу, а прямому не мешает.
#
# Обратный ход восстанавливает bool из state (active -> True, всё
# остальное -> False) — без потерь для того, что bool вообще умел
# выражать.
#
# На пилоте 11.09.2026 закрытых строк 33 (задача DRF-1660). Число здесь
# не зашито: миграция переносит столько, сколько найдёт, и печатает
# сколько.
from django.conf import settings
from django.db import migrations, models

LEGACY = "legacy_inactive_reason_unknown"
ACTIVE = "active"


def forwards(apps, schema_editor):
    ClientGoal = apps.get_model("goals", "ClientGoal")
    moved = ClientGoal.objects.filter(is_active=False).update(state=LEGACY)
    # Печать рядом с результатом: «миграция прошла» ≠ «перенесла то, что
    # нужно». Число видно в логе migrate.
    print(f"\n  goals.0006: {moved} inactive row(s) -> {LEGACY}", end="")


def backwards(apps, schema_editor):
    ClientGoal = apps.get_model("goals", "ClientGoal")
    ClientGoal.objects.exclude(state=ACTIVE).update(is_active=False)
    ClientGoal.objects.filter(state=ACTIVE).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("goals", "0005_remove_clientgoal_tenant"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. новые колонки
        migrations.AddField(
            model_name="clientgoal",
            name="state",
            field=models.CharField(
                choices=[
                    ("active", "Актуальна"),
                    ("paused", "На паузе"),
                    ("achieved", "Достигнута"),
                    ("archived", "В архиве"),
                    ("superseded", "Закрыта выбором новой цели"),
                    (
                        "legacy_inactive_reason_unknown",
                        "Закрыта до модели состояний; причина неизвестна",
                    ),
                ],
                default="active",
                help_text="Состояние жизненного цикла (§97 OD-GOAL-B); менять через goals.lifecycle",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="clientgoal",
            name="state_changed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Момент последнего перехода состояния; NULL у legacy-строк",
                null=True,
            ),
        ),
        # 2. старое ограничение снимается ДО переноса данных. Порядок
        #    важен для ОБРАТНОГО хода: reverse идёт по списку снизу вверх,
        #    и ``is_active`` возвращается с default=True у ВСЕХ строк;
        #    перенос данных обязан пройти раньше, чем старое частичное
        #    ограничение «одна is_active=True на клиента» встанет обратно.
        migrations.RemoveConstraint(
            model_name="clientgoal",
            name="clientgoal_one_active_per_client",
        ),
        migrations.RemoveIndex(
            model_name="clientgoal",
            name="clientgoal_client_active_idx",
        ),
        # 3. перенос данных — обе колонки на месте, ограничений нет
        migrations.RunPython(forwards, backwards),
        migrations.AddIndex(
            model_name="clientgoal",
            index=models.Index(
                fields=["client", "state"], name="clientgoal_client_state_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="clientgoal",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "active")),
                fields=("client",),
                name="clientgoal_one_active_per_client",
            ),
        ),
        # 4. старая колонка — последней
        migrations.RemoveField(
            model_name="clientgoal",
            name="is_active",
        ),
    ]
