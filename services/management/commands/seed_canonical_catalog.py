"""Seed the canonical service catalog from the reference JSON.

Loads ``services/seeds/canonical_catalog_2026-07.json`` (extracted from the
owner's master service list) into ``ServiceCategory`` (2-level taxonomy,
tenant-null / global) + ``ServiceTemplate`` (one row per service).

Idempotent: categories keyed by unique ``name``, templates by
``(category, name)``. Durations are left NULL (curated later); ``price`` lives
on ``RegionalPricing`` / the per-specialist ``Service``, not here.
``requires_health_check`` + ``contraindications`` are seeded from the file's
draft flags for later owner review.

See docs/CANONICAL_CATALOG_SEED_PLAN_2026-07.md (§4.1). Part of #200 / #1044.

Usage::

    python manage.py seed_canonical_catalog            # full file
    python manage.py seed_canonical_catalog --dry-run
    python manage.py seed_canonical_catalog --file <path>
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from services.models import ServiceCategory, ServiceTemplate

DEFAULT_FILE = (
    Path(__file__).resolve().parents[2] / "seeds" / "canonical_catalog_2026-07.json"
)
NAME_SHORT_MAX = 40


class Command(BaseCommand):
    help = "Seed canonical ServiceCategory + ServiceTemplate from the reference JSON."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--file", default=str(DEFAULT_FILE))
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Parse + report counts without writing to the database.",
        )

    def handle(self, *args, **options) -> None:
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"Seed file not found: {path}")
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise CommandError(f"Invalid JSON in {path}: {exc}") from exc

        if options["dry_run"]:
            cats, subs, hc = self._preview(rows)
            self.stdout.write(self.style.WARNING(
                f"[dry-run] {len(rows)} services · {cats} categories · "
                f"{subs} subcategories · health_check={hc}"
            ))
            return

        with transaction.atomic():
            n_cat, n_tpl, n_hc = self._seed(rows)

        self.stdout.write(self.style.SUCCESS(
            f"Canonical catalog seeded: +{n_cat} categories, +{n_tpl} templates "
            f"(health_check={n_hc}). Totals: categories={ServiceCategory.objects.count()}, "
            f"templates={ServiceTemplate.objects.count()}."
        ))

    @staticmethod
    def _preview(rows: list[dict]) -> tuple[int, int, int]:
        cats = {r["category"] for r in rows}
        subs = {r["subcategory"] for r in rows if r.get("subcategory")}
        hc = sum(1 for r in rows if str(r.get("requires_health_check")).lower() == "true")
        return len(cats), len(subs), hc

    def _seed(self, rows: list[dict]) -> tuple[int, int, int]:
        created_categories = 0

        # 1. Root categories (tenant-null / global taxonomy).
        top_by_no: dict[int, ServiceCategory] = {}
        for cat_no, name in sorted({(r["category_no"], r["category"]) for r in rows}):
            obj, created = ServiceCategory.objects.get_or_create(
                name=name,
                defaults={"sort_order": cat_no, "is_active": True},
            )
            top_by_no[cat_no] = obj
            created_categories += int(created)

        # 2. Subcategories (parent = root).
        sub_by_no: dict[str, ServiceCategory] = {}
        seen_subs: set[str] = set()
        for r in rows:
            sub_no = r.get("subcategory_no")
            if not sub_no or sub_no in seen_subs:
                continue
            seen_subs.add(sub_no)
            parent = top_by_no[r["category_no"]]
            try:
                order = int(sub_no.split(".")[1])
            except (IndexError, ValueError):
                order = 0
            obj, created = ServiceCategory.objects.get_or_create(
                name=r["subcategory"],
                defaults={"sort_order": order, "is_active": True, "parent": parent},
            )
            sub_by_no[sub_no] = obj
            created_categories += int(created)

        # 3. Service templates. category = subcategory row if present, else root.
        created_templates = 0
        health_check = 0
        for idx, r in enumerate(rows):
            sub_no = r.get("subcategory_no")
            category = sub_by_no[sub_no] if sub_no else top_by_no[r["category_no"]]
            hc = str(r.get("requires_health_check")).lower() == "true"
            health_check += int(hc)
            name = r["service"]
            _, created = ServiceTemplate.objects.update_or_create(
                category=category,
                name=name,
                defaults={
                    "name_short": name[:NAME_SHORT_MAX],
                    "duration_default": None,
                    "duration_min": None,
                    "duration_max": None,
                    "requires_health_check": hc,
                    "contraindications": r.get("note", "") or "",
                    "is_popular": False,
                    "sort_order": idx,
                },
            )
            created_templates += int(created)

        return created_categories, created_templates, health_check
