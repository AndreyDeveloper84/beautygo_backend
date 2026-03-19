from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0005_populate_legacy_phones"),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='phone',
            field=models.CharField(max_length=20, unique=True),
        ),
    ]
