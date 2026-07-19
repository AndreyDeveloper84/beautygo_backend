# Split per-master (AYLA-DEC-0008/D8): SpecialistProfile.yookassa_account_id.
#
# NOTE: ``makemigrations`` also detects PRE-EXISTING drift unrelated to
# this migration — ``yclients_staff_id.db_index`` (#160, never migrated)
# and cosmetic help_text drift on UserPersonalContext (0009). Those are
# intentionally NOT included here; reported to the orchestrator for the
# owning streams to migrate separately.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0013_specialistprofile_yclients_staff_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="specialistprofile",
            name="yookassa_account_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="YooKassa sub-account id for split-per-master payouts. Empty = online payment unavailable for this specialist.",
                max_length=64,
            ),
        ),
    ]
