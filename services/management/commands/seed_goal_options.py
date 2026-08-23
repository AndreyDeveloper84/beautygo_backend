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

``--prune`` (DRF-1317) — снять связь может ТОЛЬКО этот флаг
--------------------------------------------------------
Без него файл не может отозвать однажды поставленную связь. Это не
теоретическое неудобство: цель, курируемая на КОРНЕ «Массаж тела»,
доставалась всем 25 массажам ветки разом — включая массаж головы и
детский, — и убрать её правкой файла было нельзя. Перенос цели на
семантически точные подкатегории требует именно удаления трёх корневых
строк.

Флаг обязателен и удаляет только связи ОБЪЯВЛЕННЫХ в файле целей — цель,
которой в файле нет, не трогается вовсе. Каждая удаляемая строка
печатается: связи курирует владелец руками (на контуре 23.08 одна из
19 добавлена через админку, не сидом), и молчаливое удаление стёрло бы
его решение. ``--dry-run --prune`` показывает список, ничего не меняя.

Usage::

    python manage.py seed_goal_options
    python manage.py seed_goal_options --dry-run
    python manage.py seed_goal_options --dry-run --prune
    python manage.py seed_goal_options --prune
    python manage.py seed_goal_options --file <path>
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

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
        parser.add_argument(
            "--prune", action="store_true",
            help=(
                "Delete goal->category links that the file no longer declares, "
                "for the goals the file declares. Prints every deleted row."
            ),
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

        stale = self._stale_links(rows) if options["prune"] else []

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] {len(rows)} goal options · "
                f"{sum(len(r['categories']) for r in rows)} category links"
            ))
            for line in stale:
                self.stdout.write(self.style.WARNING(f"[dry-run] would unlink {line}"))
            return

        with transaction.atomic():
            n_opt, n_link = self._seed(rows)
            n_pruned = self._prune(rows) if options["prune"] else 0

        for line in stale:
            self.stdout.write(self.style.WARNING(f"unlinked {line}"))

        self.stdout.write(self.style.SUCCESS(
            f"Goal options seeded: +{n_opt} options, +{n_link} category links, "
            f"-{n_pruned} stale links. "
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
    def _stale_queryset(rows: list[dict]):
        """Связи объявленных целей, которых в файле больше нет.

        Цель, отсутствующая в файле, не попадает в выборку вообще: сид не
        вправе судить о том, чего не описывает.
        """
        declared = Q(pk__in=[])
        for r in rows:
            declared |= Q(
                goal_option__key=r["key"],
                category__name__in=r["categories"],
            )
        return (
            GoalOptionCategory.objects
            .filter(goal_option__key__in=[r["key"] for r in rows])
            .exclude(declared)
            .select_related("goal_option", "category")
        )

    @classmethod
    def _stale_links(cls, rows: list[dict]) -> list[str]:
        return [
            f"{link.goal_option.key} -> {link.category.name}"
            for link in cls._stale_queryset(rows).order_by(
                "goal_option__key", "category__name"
            )
        ]

    @classmethod
    def _prune(cls, rows: list[dict]) -> int:
        deleted, _ = cls._stale_queryset(rows).delete()
        return deleted

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
