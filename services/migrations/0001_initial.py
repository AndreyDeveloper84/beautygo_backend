"""Create Service model in services app (table already exists as users_service)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('users', '0010_remove_service_to_services_app'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='Service',
                    fields=[
                        ('id', models.BigAutoField(
                            auto_created=True, primary_key=True,
                            serialize=False, verbose_name='ID',
                        )),
                        ('name', models.CharField(max_length=100)),
                        ('description', models.TextField(blank=True)),
                        ('price', models.DecimalField(
                            decimal_places=2, max_digits=10,
                        )),
                        ('duration_minutes', models.PositiveIntegerField()),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('specialist', models.ForeignKey(
                            on_delete=django.db.models.deletion.CASCADE,
                            related_name='services',
                            to=settings.AUTH_USER_MODEL,
                        )),
                    ],
                    options={
                        'db_table': 'users_service',
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
