"""Seed Track E sub-categories for cross-domain rule engine (DRF-259).

Track E (Phase 4) cross-domain rules map nutrient deficits to specific
beauty service categories. Existing seed_service_templates covers root
categories (massage, cosmetology, ...) but not the granular children the
rule engine targets (massage with argan oil, lymphatic drainage,
brightening cosmetology, deep hydration, spa relaxation).

This command is additive and idempotent: it ensures parent categories
exist (creating "massage" / "cosmetology" / "spa" if missing) and then
creates the six Track E child slugs without touching unrelated rows.

Usage:
    python manage.py seed_track_e_categories
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from services.models import ServiceCategory


# (slug, name, parent_slug) for parent rows that may not yet exist.
# Mirrors the slugs seed_service_templates uses where applicable, so
# both seeds converge on the same canonical row.
PARENT_CATEGORIES: list[tuple[str, str, str | None]] = [
    ("massage", "Массаж", None),
    ("cosmetology", "Косметология", None),
    ("spa", "СПА", None),
]


# (slug, name, parent_slug, sort_order, icon).
# sort_order chosen to put Track E children after typical seed_service_templates
# children (which start at 10) so they don't reorder existing UI lists.
TRACK_E_CHILDREN: list[tuple[str, str, str, int, str]] = [
    (
        "massage-argan-oil", "Массаж с аргановым маслом",
        "massage", 100, "hand",
    ),
    (
        "massage-lymph-drainage", "Лимфодренажный массаж",
        "massage", 110, "hand",
    ),
    (
        "cosmetology-brightening", "Осветление кожи (Vitamin C)",
        "cosmetology", 100, "sparkles",
    ),
    (
        "cosmetology-deep-hydration", "Глубокое увлажнение",
        "cosmetology", 110, "sparkles",
    ),
    (
        "spa-relaxation", "Расслабляющие СПА-программы",
        "spa", 10, "leaf",
    ),
]


class Command(BaseCommand):
    help = "Seed Track E sub-categories for cross-domain rule engine."

    @transaction.atomic
    def handle(self, *_args, **_options):
        # Parent rows first — defensive against running on a DB where
        # seed_service_templates has not been executed yet.
        parent_by_slug: dict[str, ServiceCategory] = {}
        for slug, name, parent_slug in PARENT_CATEGORIES:
            parent_obj = (
                parent_by_slug.get(parent_slug) if parent_slug else None
            )
            obj, created = ServiceCategory.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "parent": parent_obj,
                    "is_active": True,
                },
            )
            parent_by_slug[slug] = obj
            self.stdout.write(
                self.style.SUCCESS("created parent ") if created
                else self.style.NOTICE("kept parent ")
            )
            self.stdout.write(f"  {slug}\n")

        # Track E children. get_or_create keeps reruns safe; we never
        # update fields on existing rows so a curator can rename a row
        # in the admin without our seed clobbering the change.
        for slug, name, parent_slug, sort_order, icon in TRACK_E_CHILDREN:
            parent = parent_by_slug.get(parent_slug)
            if parent is None:
                # Should not happen — parents above cover the slugs we use.
                self.stderr.write(
                    f"missing parent {parent_slug} for {slug}; skipping",
                )
                continue
            _obj, created = ServiceCategory.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "parent": parent,
                    "sort_order": sort_order,
                    "icon": icon,
                    "is_active": True,
                },
            )
            self.stdout.write(
                self.style.SUCCESS("created child  ") if created
                else self.style.NOTICE("kept child    ")
            )
            self.stdout.write(f"  {slug}\n")

        self.stdout.write(
            self.style.SUCCESS(
                f"done · {len(TRACK_E_CHILDREN)} Track E children + "
                f"{len(PARENT_CATEGORIES)} parents ensured"
            )
        )
