"""``manage.py intake_csv`` — seed pilot catalog from a CSV (S3C PR4).

Loads a bootstrap CSV (e.g. a ``mysite`` last-sync export) into
``DraftSalonService`` rows via the shared IntakePipeline — the pilot path
that does NOT wait on the YClients licence. Confirm the drafts afterwards
with ``intake_confirm``.

    manage.py intake_csv --tenant penza-salon --file pilot_catalog.csv
    manage.py intake_csv --tenant penza-salon --file pilot_catalog.csv --dry-run
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from services.integrations.intake.pipeline import import_catalog
from services.integrations.intake.sources import CsvIntakeError, CsvSource
from services.models import DraftSalonService
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Seed pilot catalog drafts from a bootstrap CSV."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant slug.")
        parser.add_argument("--file", required=True, help="Path to the CSV file.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Parse and report without writing drafts.",
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(slug=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant slug '{options['tenant']}' not found.")

        path = options["file"]
        if not os.path.isfile(path):
            raise CommandError(f"CSV file not found: {path}")

        source = CsvSource(path)

        try:
            if options["dry_run"]:
                services = source.fetch_services()
                self.stdout.write(f"[dry-run] {len(services)} service row(s) parsed:")
                for rec in services:
                    self.stdout.write(
                        f"  - {rec.external_service_id} {rec.name!r} "
                        f"dur={rec.duration_min} price={rec.price_min} "
                        f"staff={list(rec.external_staff_ids)}"
                    )
                return
            summary = import_catalog(
                source, tenant,
                external_source=DraftSalonService.ExternalSource.CSV,
            )
        except CsvIntakeError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"Loaded CSV: {summary.services_created} created, "
            f"{summary.services_updated} updated, {summary.services_skipped} skipped."
        ))
