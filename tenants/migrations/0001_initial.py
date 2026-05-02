"""Initial Tenant table (DRF-242.1).

Pure additive — no FK rewires yet. Subsequent tickets convert
``ai.Conversation.tenant_id`` (UUIDField) into a FK to this table and
add tenant FKs to other models.
"""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Tenant",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "slug",
                    models.SlugField(
                        max_length=50,
                        unique=True,
                        help_text=(
                            "Lowercase identifier used in URLs and the X-Tenant "
                            "header. Letters, digits, hyphen, underscore. Must "
                            "start with a letter or digit. Cannot be changed "
                            "after creation."
                        ),
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=200,
                        help_text="Human-readable name shown in admin and billing.",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "False hides the tenant from default queries. Use "
                            "this to soft-disable a tenant without dropping its "
                            "data — billing freezes, scoping middleware "
                            "returns 403."
                        ),
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Тенант",
                "verbose_name_plural": "Тенанты",
                "ordering": ["name"],
            },
        ),
        migrations.AddIndex(
            model_name="tenant",
            index=models.Index(
                fields=["is_active", "slug"], name="tenants_ten_is_acti_2c1ec7_idx",
            ),
        ),
    ]
