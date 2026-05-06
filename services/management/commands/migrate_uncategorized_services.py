"""Backfill ServiceCategory for legacy Service rows (DRF-259).

Older Service rows were created with category=None (free-text name only).
For Track E cross-domain rule engine — and for the catalog filters that
already exist — every Service should resolve to a category. This command
runs regex matching over Service.name and assigns the closest known slug.

Match priority is encoded by ordering: more specific patterns must come
first. "Массаж с аргановым маслом" → massage-argan-oil wins over the
generic "massage" fallback. Unmatched names are listed in stdout for a
human to curate via the Django admin.

Usage:
    python manage.py migrate_uncategorized_services           # apply
    python manage.py migrate_uncategorized_services --dry-run # preview
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from services.models import Service, ServiceCategory


# (compiled_regex, slug) — order matters. Specific patterns first.
# Patterns are case-insensitive, run against Service.name lowercased.
PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Track E specific massages
    (
        re.compile(r"арган(ов(ое|ым|ого|ой)?\s+маслом|ово?е?\s+масло)"),
        "massage-argan-oil",
    ),
    (
        re.compile(r"лимфо\s*дренаж|лимфо-дренаж"),
        "massage-lymph-drainage",
    ),
    # Cosmetology Track E specifics
    (
        re.compile(r"осветлен|витамин\s*c|brightening"),
        "cosmetology-brightening",
    ),
    (
        re.compile(r"глубокое\s+увлажнен|deep\s+hydration"),
        "cosmetology-deep-hydration",
    ),
    # SPA
    (
        re.compile(r"спа[-\s]+программ|расслабляющ.*спа|spa-relaxation"),
        "spa-relaxation",
    ),
    # Generic fallbacks (must come after Track E specific patterns)
    (re.compile(r"массаж"), "massage"),
    (re.compile(r"чистка\s+лица"), "facial-cleansing"),
    (re.compile(r"пилинг"), "peeling"),
    (re.compile(r"маникюр"), "manicure"),
    (re.compile(r"педикюр"), "pedicure"),
    (re.compile(r"наращивание\s+ногтей"), "nail-extensions"),
    (re.compile(r"стрижк"), "haircut"),
    (re.compile(r"окрашиван"), "coloring"),
    (re.compile(r"укладк"), "styling"),
    (re.compile(r"коррекц.*бровей"), "brow-shaping"),
    (re.compile(r"ламинирован.*бров"), "brow-lamination"),
    (re.compile(r"наращиван.*ресниц"), "lash-extensions"),
    (re.compile(r"шугаринг"), "sugaring"),
    (re.compile(r"восков.*депиляц|воск"), "waxing"),
    (re.compile(r"макияж"), "makeup"),
]


class Command(BaseCommand):
    help = "Backfill Service.category for rows with category=None via regex."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Preview matches without writing to the DB.",
        )

    def handle(self, *_args, **options):
        dry_run = options["dry_run"]

        # Cache categories by slug — single query.
        cat_by_slug: dict[str, ServiceCategory] = {
            c.slug: c for c in ServiceCategory.objects.all()
        }

        uncategorized = Service.objects.filter(category__isnull=True)
        matched = 0
        unmatched: list[str] = []

        with transaction.atomic():
            for svc in uncategorized:
                slug = self._match_slug(svc.name)
                if slug is None or slug not in cat_by_slug:
                    unmatched.append(svc.name)
                    continue
                if not dry_run:
                    svc.category = cat_by_slug[slug]
                    svc.save(update_fields=["category", "updated_at"])
                matched += 1
                prefix = "[dry-run] " if dry_run else ""
                self.stdout.write(
                    f"{prefix}{svc.name!r} → {slug}\n"
                )

            if dry_run:
                # Roll back inside the transaction by raising — but we
                # never wrote anything, so just exit the with-block.
                pass

        self.stdout.write(
            self.style.SUCCESS(
                f"\nmatched: {matched}, unmatched: {len(unmatched)}"
                f"{' (dry-run, no changes)' if dry_run else ''}"
            )
        )
        for name in unmatched:
            self.stdout.write(f"  unmatched: {name}\n")

    @staticmethod
    def _match_slug(name: str) -> str | None:
        lowered = name.lower()
        for pattern, slug in PATTERNS:
            if pattern.search(lowered):
                return slug
        return None
