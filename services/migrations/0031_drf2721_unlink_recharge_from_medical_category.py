"""DRF-2721 — the goal «Восстановить силы» no longer leads into the medical category.

The goal seed linked ``recharge`` to the whole category «Медико-эстетические
и смежные услуги» (canon category 19). All 20 canonical services there carry
``requires_health_check`` — «Удаление новообразований», «Обработка
диабетической стопы» among them — so the goal chip recommended medical
services as a way to «recharge». Owner's word 02.10: remove the link now; the
services themselves stay in the catalog.

Why a data migration and not the seed. ``seed_goal_options`` only creates
missing links; an existing one is removed by ``--prune`` alone, and the deploy
runs nothing but ``migrate`` (``entrypoint.sh``) — there is no shell on the
stand to run the command from. Editing the seed file (same commit) keeps the
link from being created again; this migration removes the row that is already
there.

Exactly one pair is touched: goal ``recharge`` × the category of that name.
Other goals' links to the category, if the owner added any in the admin, and
``recharge``'s other links are left alone. No row — nothing happens.

The reverse is a no-op on purpose: rolling the schema back must not put an
unsafe recommendation back.
"""
from django.db import migrations

GOAL_KEY = "recharge"
CATEGORY_NAME = "Медико-эстетические и смежные услуги"


def unlink_recharge_from_medical_category(apps, schema_editor):
    link = apps.get_model("services", "GoalOptionCategory")
    link.objects.filter(
        goal_option__key=GOAL_KEY, category__name=CATEGORY_NAME,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0030_drf2612_provenance_protect"),
    ]

    operations = [
        migrations.RunPython(
            unlink_recharge_from_medical_category, migrations.RunPython.noop,
        ),
    ]
