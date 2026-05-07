"""Idempotent backfill of tenant FKs across the tenant-scoped models (DRF-242.4).

Backfill order matters because some models inherit tenant from their parent:

    User              ← assigned default tenant
    SpecialistProfile ← copied from user.tenant
    Conversation      ← copied from user.tenant
    FoodScan          ← copied from user.tenant
    Appointment       ← copied from specialist.tenant

The default tenant is created if missing (slug from
``settings.MULTI_TENANT_DEFAULT_SLUG`` or the ``--slug`` CLI flag).

Idempotent — safe to re-run. Each step skips rows that already have a
tenant set, so partial runs (interrupted by network / OOM) resume
cleanly. ``--dry-run`` reports what *would* change without writing.

Usage:

    python manage.py backfill_tenants                        # uses settings default
    python manage.py backfill_tenants --slug formula --name "Формула тела"
    python manage.py backfill_tenants --dry-run

Out of scope: scoping middleware enforcement (lives in DRF-242.5 strict
flag) and per-row attribution — this assigns *every* legacy row to the
single default tenant. Multi-tenant deployments will need a separate
per-row attribution pass before flipping strict mode on.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Backfill tenant FK on all tenant-scoped models. Idempotent."

    def add_arguments(self, parser):
        default_slug = getattr(settings, "MULTI_TENANT_DEFAULT_SLUG", "formula")
        parser.add_argument(
            "--slug", default=default_slug,
            help=f"Slug for the default tenant (default: {default_slug!r}).",
        )
        parser.add_argument(
            "--name", default=None,
            help=(
                "Name for the default tenant when creating it. Required only if "
                "the slug doesn't already exist."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print row counts without writing anything.",
        )

    def handle(self, *, slug: str, name: str | None, dry_run: bool, **opts):
        from tenants.models import Tenant
        from users.models import SpecialistProfile, User
        from ai.models import Conversation
        from nutrition.models import FoodScan
        from appointments.models import Appointment
        from services.models import Service, ServiceCategory
        from reviews.models import Review

        tenant = Tenant.all_objects.filter(slug=slug).first()
        if tenant is None:
            if dry_run:
                self.stdout.write(self.style.WARNING(
                    f"[dry-run] would create Tenant(slug={slug!r}, name={(name or slug).title()!r})"
                ))
                return
            tenant = Tenant.objects.create(slug=slug, name=name or slug.title())
            self.stdout.write(self.style.SUCCESS(
                f"Created tenant {tenant.slug!r} (id={tenant.id})"
            ))
        else:
            self.stdout.write(f"Using existing tenant {tenant.slug!r} (id={tenant.id})")

        steps: list[tuple[str, int]] = []

        with transaction.atomic():
            # 1. Users without tenant → default.
            users_qs = User.objects.filter(tenant__isnull=True)
            count = users_qs.count()
            if not dry_run and count:
                users_qs.update(tenant=tenant)
            steps.append(("User", count))

            # 2. SpecialistProfile inherits from user.tenant.
            sp_qs = SpecialistProfile.objects.filter(tenant__isnull=True)
            count = sp_qs.count()
            if not dry_run and count:
                # After step 1 every user has a tenant — flat update is safe.
                for sp in sp_qs.select_related("user").only("id", "user__tenant_id"):
                    SpecialistProfile.objects.filter(pk=sp.pk).update(
                        tenant_id=sp.user.tenant_id,
                    )
            steps.append(("SpecialistProfile", count))

            # 3. Conversation inherits from user.tenant.
            conv_qs = Conversation.all_objects.filter(tenant__isnull=True)
            count = conv_qs.count()
            if not dry_run and count:
                for conv in conv_qs.select_related("user").only("id", "user__tenant_id"):
                    Conversation.all_objects.filter(pk=conv.pk).update(
                        tenant_id=conv.user.tenant_id,
                    )
            steps.append(("Conversation", count))

            # 4. FoodScan inherits from user.tenant.
            fs_qs = FoodScan.objects.filter(tenant__isnull=True)
            count = fs_qs.count()
            if not dry_run and count:
                for fs in fs_qs.select_related("user").only("id", "user__tenant_id"):
                    FoodScan.objects.filter(pk=fs.pk).update(
                        tenant_id=fs.user.tenant_id,
                    )
            steps.append(("FoodScan", count))

            # 5. Appointment inherits from specialist.tenant.
            appt_qs = Appointment.objects.filter(tenant__isnull=True)
            count = appt_qs.count()
            if not dry_run and count:
                for a in appt_qs.select_related("specialist").only("id", "specialist__tenant_id"):
                    Appointment.objects.filter(pk=a.pk).update(
                        tenant_id=a.specialist.tenant_id,
                    )
            steps.append(("Appointment", count))

            # 6. ServiceCategory has no parent FK to scope from — assign
            #    every legacy row to the default tenant. Multi-tenant
            #    deployments will need a separate per-row attribution
            #    pass before flipping strict mode on.
            cat_qs = ServiceCategory.objects.filter(tenant__isnull=True)
            count = cat_qs.count()
            if not dry_run and count:
                cat_qs.update(tenant=tenant)
            steps.append(("ServiceCategory", count))

            # 7. Service inherits from specialist.tenant (DRF-242.6).
            svc_qs = Service.objects.filter(tenant__isnull=True)
            count = svc_qs.count()
            if not dry_run and count:
                for s in svc_qs.select_related("specialist").only("id", "specialist__tenant_id"):
                    Service.objects.filter(pk=s.pk).update(
                        tenant_id=s.specialist.tenant_id,
                    )
            steps.append(("Service", count))

            # 8. Review inherits from specialist.tenant (DRF-242.6).
            review_qs = Review.objects.filter(tenant__isnull=True)
            count = review_qs.count()
            if not dry_run and count:
                for r in review_qs.select_related("specialist").only("id", "specialist__tenant_id"):
                    Review.objects.filter(pk=r.pk).update(
                        tenant_id=r.specialist.tenant_id,
                    )
            steps.append(("Review", count))

            if dry_run:
                # Roll the whole atomic block back even though we didn't
                # write — keeps the message honest if the runner expected
                # zero side effects.
                transaction.set_rollback(True)

        prefix = "[dry-run] would update" if dry_run else "Updated"
        for label, count in steps:
            self.stdout.write(self.style.SUCCESS(f"{prefix} {label}: {count}"))
