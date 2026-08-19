"""Seed curated goal options + goal->category mapping from JSON (DRF-1190).

Loads ``services/seeds/goal_options_2026-08.json`` into ``GoalOption``
(keyed by unique ``key``) and ``GoalOptionCategory`` (keyed by
``(goal_option, category)``). Categories resolve by exact ``name``
against the canonical taxonomy seeded by ``seed_canonical_catalog``.

Idempotent. Fails loudly listing every unresolved category name — the
mapping is curated owner data; a silent skip would ship a chip that
resolves to nothing. ``--dry-run`` previews without writing.

Deactivated options/mappings are NOT removed by reseeding (owner may
have hand-tuned rows via admin); the seed only creates/updates.

Usage::

    python manage.py seed_goal_options
    python manage.py seed_goal_options --dry-run
    python manage.py seed_goal_options --file <path>
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from services.models import GoalOption, GoalOptionCategory, ServiceCategory

DEFAULT_FILE = (
    Path(__file__).resolve().parents[2] / "seeds" / "goal_options_2026-08.json"
)


class Command(BaseCommand):
    help = "Seed GoalOption + GoalOptionCategory from the curated JSON."

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

        unresolved = self._unresolved_categories(rows)
        if unresolved:
            raise CommandError(
                "Unresolved ServiceCategory names in seed (fix the file or "
                "seed the canonical catalog first): " + ", ".join(sorted(unresolved))
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] {len(rows)} goal options · "
                f"{sum(len(r['categories']) for r in rows)} category links"
            ))
            return

        with transaction.atomic():
            n_opt, n_link = self._seed(rows)

        self.stdout.write(self.style.SUCCESS(
            f"Goal options seeded: +{n_opt} options, +{n_link} category links. "
            f"Totals: options={GoalOption.objects.count()}, "
            f"links={GoalOptionCategory.objects.count()}."
        ))

    @staticmethod
    def _unresolved_categories(rows: list[dict]) -> set[str]:
        names = {name for r in rows for name in r["categories"]}
        existing = set(
            ServiceCategory.objects.filter(name__in=names).values_list("name", flat=True)
        )
        return names - existing

    @staticmethod
    def _seed(rows: list[dict]) -> tuple[int, int]:
        categories_by_name = {
            c.name: c
            for c in ServiceCategory.objects.filter(
                name__in={n for r in rows for n in r["categories"]}
            )
        }
        created_options = 0
        created_links = 0
        for r in rows:
            option, created = GoalOption.objects.update_or_create(
                key=r["key"],
                defaults={
                    "label": r["label"],
                    "sort_order": r.get("sort_order", 0),
                    "is_active": True,
                },
            )
            created_options += int(created)
            for idx, cat_name in enumerate(r["categories"]):
                _, created = GoalOptionCategory.objects.get_or_create(
                    goal_option=option,
                    category=categories_by_name[cat_name],
                    defaults={"sort_order": idx},
                )
                created_links += int(created)
        return created_options, created_links
