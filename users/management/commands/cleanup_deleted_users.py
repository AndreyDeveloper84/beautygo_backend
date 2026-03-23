"""Hard delete users whose accounts were soft-deleted over 30 days ago."""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from users.models import User

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 30


class Command(BaseCommand):
    help = "Permanently delete user accounts that were soft-deleted over 30 days ago."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting.",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=GRACE_PERIOD_DAYS)
        users = User.objects.filter(
            is_active=False,
            deleted_at__isnull=False,
            deleted_at__lte=cutoff,
        )

        count = users.count()
        if count == 0:
            self.stdout.write("No accounts to clean up.")
            return

        if options["dry_run"]:
            self.stdout.write(f"Would delete {count} account(s):")
            for u in users:
                self.stdout.write(f"  id={u.pk} deleted_at={u.deleted_at}")
            return

        deleted, _ = users.delete()
        self.stdout.write(
            self.style.SUCCESS(f"Permanently deleted {deleted} account(s)."),
        )
        logger.info("Hard-deleted %d user accounts (grace period expired)", deleted)
