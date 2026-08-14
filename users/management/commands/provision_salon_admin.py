"""Give a salon an administrator (DRF-1062).

Without this, everything else in DRF-1062 is unreachable. ``IsTenantAdmin``
needs an active ``TenantUserRelationship`` with role ``admin`` for the
addressed tenant, and the pilot salon has none — the audit of 2026-08-14
found zero administrators for it. Shipping the admin surface without a way
to create that row would repeat the mistake that left the bot's Mini App
built, deployed and unusable behind an empty ``TenantStaff`` table.

Idempotent by construction: re-running reuses the user and the grant. The
phone number is an argument, never a literal in this repository — it is
personal data and belongs in the operator's command line, not in git.

Deliberately refuses to act when a *revoked* relationship exists: someone
removed that person's access on purpose, and silently restoring it from a
provisioning script is not a decision a script should make.

    manage.py provision_salon_admin --phone +79001234567 --tenant formula-tela
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tenants.models import Tenant
from users.models import TenantUserRelationship, User


class Command(BaseCommand):
    help = "Create or reuse a salon administrator and grant them tenant admin."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--phone", required=True,
            help="Login phone (OTP is the only auth route for humans).",
        )
        parser.add_argument(
            "--tenant", required=True,
            help="Tenant slug, e.g. formula-tela.",
        )
        parser.add_argument(
            "--name", default="",
            help="Optional display name for a newly created account.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change and exit without writing.",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        phone = options["phone"].strip()
        slug = options["tenant"].strip()
        dry_run = options["dry_run"]

        try:
            tenant = Tenant.objects.get(slug=slug)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant '{slug}' does not exist.")

        user = User.objects.filter(phone=phone).first()
        created_user = user is None

        if created_user:
            if dry_run:
                self.stdout.write(f"would create user for phone in tenant {slug}")
            else:
                user = User.objects.create_user(
                    username=phone,
                    phone=phone,
                    role="admin",
                    first_name=options["name"],
                    is_verified=True,
                )
        else:
            # An existing account keeps its role: a client who also
            # administers a salon is a normal situation, and the grant
            # below is what actually authorises them.
            self.stdout.write(f"reusing existing account (id={user.pk})")

        if dry_run and created_user:
            self.stdout.write(self.style.WARNING("dry-run: nothing written"))
            return

        revoked = TenantUserRelationship.objects.filter(
            user=user, tenant=tenant, is_active=False,
        ).exists()
        active = TenantUserRelationship.objects.filter(
            user=user, tenant=tenant, is_active=True,
        ).first()

        if revoked and not active:
            raise CommandError(
                "This user's access to the tenant was revoked. Restoring it "
                "is a deliberate decision — grant it explicitly instead of "
                "through provisioning."
            )

        if active and active.role == TenantUserRelationship.Role.ADMIN:
            self.stdout.write(self.style.SUCCESS(
                f"already an administrator of {slug} — nothing to do"
            ))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"dry-run: would grant admin on {slug}"
            ))
            return

        if active:
            # Promote in place: the partial unique constraint allows only
            # one active relationship per (user, tenant), so a second row
            # would fail rather than upgrade them.
            active.role = TenantUserRelationship.Role.ADMIN
            active.save(update_fields=["role"])
            self.stdout.write(self.style.SUCCESS(
                f"promoted existing relationship to admin on {slug}"
            ))
        else:
            TenantUserRelationship.objects.create(
                user=user,
                tenant=tenant,
                role=TenantUserRelationship.Role.ADMIN,
                is_active=True,
                granted_by=TenantUserRelationship.GrantedBy.ADMIN,
            )
            self.stdout.write(self.style.SUCCESS(
                f"granted admin on {slug}"
            ))

        self.stdout.write(
            "Login route: OTP to the provided phone. Send X-Tenant: "
            f"{tenant.id} (or rely on the JWT tenant claim) with "
            "X-App-Type: pro when calling the salon-admin endpoints."
        )
