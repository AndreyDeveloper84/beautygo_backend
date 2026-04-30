"""Add User.is_proxy for service-account-created proxy users (DRF-246).

Used when MAX bot calls Ayla nutrition API on behalf of a BotUser. Username
is namespaced (e.g. 'bot:12345'). Phase C will link proxy to real account.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_favoritespecialist'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='is_proxy',
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Proxy user created via service-to-service auth (e.g. MAX bot calling "
                    "Ayla nutrition API on behalf of a BotUser). Username is namespaced "
                    "(e.g. 'bot:12345'). Phase C migration links proxy to a real account."
                ),
            ),
        ),
    ]
