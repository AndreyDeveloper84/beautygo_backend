"""TenantUserRelationship model + backfill from User.tenant (#246).

Schema variant β (partial unique + history). Backfills one TUR per
existing User.tenant FK pair with role derived from User.role:
- client → customer
- specialist → staff
- admin → admin

After this migration:
- TenantUserRelationship.objects.filter(is_active=True).count()
  == User.objects.exclude(tenant=None).count()
- `User.tenant` FK stays on the model as a denormalized pointer
  (deprecated for customers; semantically meaningful only for staff).
  Dropping it is a Sprint 2 refactor.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


# Map User.role → TUR.role at backfill time.
_ROLE_MAP = {
    "client": "customer",
    "specialist": "staff",
    "admin": "admin",
}


def backfill_tur_from_user_tenant(apps, schema_editor):
    """Create one active TUR per User row that has a non-null tenant.

    Idempotent: on re-run, skip users that already have an active
    TUR for the same (user, tenant) pair — the partial unique
    constraint would catch the dupe anyway, but the explicit filter
    keeps the runtime tidy and the row count predictable.
    """
    User = apps.get_model("users", "User")
    TUR = apps.get_model("users", "TenantUserRelationship")

    existing_pairs = set(
        TUR.objects.filter(is_active=True).values_list(
            "user_id", "tenant_id",
        )
    )

    to_create = []
    qs = User.objects.exclude(tenant__isnull=True).only(
        "id", "tenant_id", "role",
    )
    for user in qs.iterator(chunk_size=1000):
        pair = (user.id, user.tenant_id)
        if pair in existing_pairs:
            continue
        role = _ROLE_MAP.get(user.role, "customer")
        to_create.append(TUR(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=role,
            is_active=True,
            granted_by="system",
        ))

    if to_create:
        TUR.objects.bulk_create(to_create, batch_size=500)


def reverse_noop(apps, schema_editor):
    """Reverse intentionally a no-op. Dropping the table is what the
    framework's reverse-CreateModel does — this RunPython doesn't
    need to inverse the data write (rows die with the table)."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0011_specialistprofile_booking_source"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="TenantUserRelationship",
            fields=[
                ("id", models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                )),
                ("role", models.CharField(
                    choices=[
                        ("customer", "Customer"),
                        ("staff", "Staff"),
                        ("admin", "Admin"),
                    ],
                    default="customer",
                    help_text=(
                        "customer: granted on booking via Variant E. "
                        "staff: specialist working in the salon. "
                        "admin: tenant owner / manager."
                    ),
                    max_length=16,
                )),
                ("is_active", models.BooleanField(default=True, db_index=True)),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                ("granted_by", models.CharField(
                    choices=[
                        ("self", "Self (user-initiated)"),
                        ("admin", "Admin (tenant-initiated)"),
                        ("system", "System (backfill / cascade)"),
                    ],
                    default="self",
                    max_length=16,
                )),
                ("revoked_at", models.DateTimeField(
                    null=True, blank=True, db_index=True,
                )),
                ("revoke_reason", models.CharField(
                    blank=True, default="", max_length=128,
                )),
                ("tenant", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="user_relationships",
                    to="tenants.tenant",
                )),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="tenant_relationships",
                    to="users.user",
                )),
            ],
            options={
                "verbose_name": "Tenant ↔ User relationship",
                "verbose_name_plural": "Tenant ↔ User relationships",
            },
        ),
        migrations.AddConstraint(
            model_name="tenantuserrelationship",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("user", "tenant"),
                name="tur_unique_active",
            ),
        ),
        migrations.AddIndex(
            model_name="tenantuserrelationship",
            index=models.Index(
                fields=["user", "tenant"], name="tur_lookup_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="tenantuserrelationship",
            index=models.Index(
                fields=["user", "is_active"], name="tur_user_active_idx",
            ),
        ),
        migrations.RunPython(
            backfill_tur_from_user_tenant,
            reverse_noop,
        ),
    ]
