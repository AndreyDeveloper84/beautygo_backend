"""Remove Service model from users app (moved to services app)."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0009_add_devicetoken'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(
                    name='Service',
                ),
            ],
            database_operations=[],
        ),
    ]
