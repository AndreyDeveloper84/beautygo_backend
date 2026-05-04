"""Seed/refresh the Beverage catalog (DRF-301).

Idempotent — re-running updates existing rows by slug instead of creating
duplicates. Content edits made in admin (renaming, alias tweaks,
coefficient corrections) are *overwritten* on re-seed; for sticky edits
either remove the slug from ``BEVERAGES`` or use ``--only-new`` to skip
existing rows.

Usage:
    python manage.py seed_beverages              # upsert all
    python manage.py seed_beverages --only-new   # skip existing slugs
    python manage.py seed_beverages --dry-run    # report without writing
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from nutrition.data.beverages_seed import BEVERAGES, beverage_row_to_dict
from nutrition.models import Beverage


class Command(BaseCommand):
    help = "Seed or update the Beverage catalog from beverages_seed.py."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only-new",
            action="store_true",
            help="Skip slugs that already exist (preserve manual admin edits).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without touching the database.",
        )

    def handle(self, *args, **opts):
        only_new: bool = opts["only_new"]
        dry_run: bool = opts["dry_run"]

        existing_slugs = set(Beverage.objects.values_list("slug", flat=True))

        created = 0
        updated = 0
        skipped = 0

        with transaction.atomic():
            for row in BEVERAGES:
                if only_new and row.slug in existing_slugs:
                    skipped += 1
                    continue

                if dry_run:
                    if row.slug in existing_slugs:
                        updated += 1
                    else:
                        created += 1
                    continue

                _, was_created = Beverage.objects.update_or_create(
                    slug=row.slug,
                    defaults=beverage_row_to_dict(row),
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

            if dry_run:
                # Roll back any incidental writes — defensive, the loop
                # above doesn't write under --dry-run but this guarantees
                # the contract.
                transaction.set_rollback(True)

        msg = (
            f"seed_beverages: created={created}, updated={updated}, "
            f"skipped={skipped}, total_in_seed={len(BEVERAGES)}"
        )
        if dry_run:
            msg = "[dry-run] " + msg
        self.stdout.write(self.style.SUCCESS(msg))
