"""``manage.py intake_confirm`` — confirm YClients intake drafts (S3C PR3).

Materializes pending ``DraftSalonService`` rows into bookable catalog
(SalonService + SpecialistService) for a pilot salon. Human-in-the-loop:
run per-service with its staff, or batch-confirm relying on each draft's
``raw_payload['staff_ids']``.

Examples::

    manage.py intake_confirm --tenant penza-salon --draft-id <uuid> --staff 10 --staff 11
    manage.py intake_confirm --tenant penza-salon --dry-run
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from services.integrations.intake.confirm import DraftNotConfirmable, confirm_draft
from services.models import DraftSalonService, ServiceCategory
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Confirm pending YClients intake drafts into bookable catalog rows."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant slug.")
        parser.add_argument(
            "--draft-id", default=None,
            help="Confirm only this draft (UUID). Default: all pending for the tenant.",
        )
        parser.add_argument(
            "--category", default=None,
            help="Fallback ServiceCategory id for off-taxonomy drafts (no template).",
        )
        parser.add_argument(
            "--staff", action="append", default=None, dest="staff",
            help="YClients staff id to make bookable (repeatable). "
                 "Default: each draft's raw_payload['staff_ids'].",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List eligible drafts without writing.",
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(slug=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant slug '{options['tenant']}' not found.")

        fallback_category = None
        if options["category"]:
            try:
                fallback_category = ServiceCategory.objects.get(pk=options["category"])
            except (ServiceCategory.DoesNotExist, ValueError, TypeError, ValidationError):
                raise CommandError(f"ServiceCategory '{options['category']}' not found.")

        drafts = DraftSalonService.objects.filter(
            tenant=tenant, status=DraftSalonService.Status.PENDING,
        )
        if options["draft_id"]:
            drafts = drafts.filter(pk=options["draft_id"])

        drafts = list(drafts)
        if not drafts:
            self.stdout.write("No pending drafts to confirm.")
            return

        if options["dry_run"]:
            self.stdout.write(f"[dry-run] {len(drafts)} draft(s) would be confirmed:")
            for d in drafts:
                self.stdout.write(f"  - {d.external_service_id} {d.external_name!r}")
            return

        confirmed = skipped = bookable = 0
        for draft in drafts:
            try:
                result = confirm_draft(
                    draft, staff_ids=options["staff"],
                    fallback_category=fallback_category,
                )
            except DraftNotConfirmable as exc:
                skipped += 1
                self.stderr.write(f"SKIP {draft.external_service_id}: {exc}")
                continue
            confirmed += 1
            bookable += result.specialist_services_created
            if result.unmatched_staff:
                self.stderr.write(
                    f"  {draft.external_service_id}: unmatched staff "
                    f"{', '.join(result.unmatched_staff)}"
                )
            if result.specialist_services_skipped_no_price:
                self.stderr.write(
                    f"  {draft.external_service_id}: "
                    f"{result.specialist_services_skipped_no_price} specialist(s) "
                    "skipped (draft has no price)"
                )
            if result.specialist_services_skipped_invalid:
                self.stderr.write(
                    f"  {draft.external_service_id}: "
                    f"{result.specialist_services_skipped_invalid} specialist(s) "
                    "skipped (no resolvable duration)"
                )

        self.stdout.write(self.style.SUCCESS(
            f"Confirmed {confirmed} draft(s), {bookable} bookable specialist-service(s), "
            f"{skipped} skipped."
        ))
