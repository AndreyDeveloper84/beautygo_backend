"""Seed the canonical service catalog from the reference JSON.

Loads ``services/seeds/canonical_catalog_2026-07.json`` (extracted from the
owner's master service list) into ``ServiceCategory`` (2-level taxonomy,
tenant-null / global) + ``ServiceTemplate`` (one row per service).

Idempotent: categories keyed by unique ``name``, templates by
``(category, name)``. Durations are left NULL (curated later); ``price`` lives
on ``RegionalPricing`` / the per-specialist ``Service``, not here.
``requires_health_check`` + ``contraindications`` are seeded from the file's
draft flags for later owner review.

Существующие строки сид не трогает (DRF-2663)
---------------------------------------------
Всё, что сид кладёт в строку справочника, дальше живёт под рукой человека:
длительности куратор заполняет, флаг гейта здоровья и противопоказания
владелец просматривает и подтверждает (§95, ``health_check_origin``),
``is_popular``/``sort_order`` правятся прямо в списке админки, одобрение
ставит человек или правило. Поэтому шаблон заводится ``get_or_create`` —
так же, как категории в этой же команде и как у ``seed_cross_domain_rules``.

До DRF-2663 здесь стоял ``update_or_create``, и повторный прогон:
возвращал флаг гейта к черновику файла, оставляя рядом «подтвердил
человек»; обнулял длительности; переписывал одобрение человека на
одобрение правилом; и ставил ``approved_at`` датой КАЖДОГО прогона — так
что отметка «одобрено» никогда не выглядела старой, и расхождение нельзя
было заметить по данным.

Чего эта форма НЕ делает: новое издание файла (исправленная длительность,
флаг, текст) на уже заведённые строки НЕ ложится — пути у него нет, и кто
его применяет, не решено (вопрос владельцу). Отчёт печатает, сколько
существующих строк оставлено как есть, чтобы пропуск был виден. Черновые
(``PROVISIONAL``) строки сид тоже не одобряет: канон с тем же именем завёл
оператор на ходу (§93) или человек вернул в черновик, и одобрять его —
решение куратора. ``approved_at`` пишется один раз — в момент одобрения.

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
from django.utils import timezone

from services.models import ServiceCategory, ServiceTemplate

DEFAULT_FILE = (
    Path(__file__).resolve().parents[2] / "seeds" / "canonical_catalog_2026-07.json"
)
NAME_SHORT_MAX = 40

#: Имя правила одобрения. Версией служит имя файла-источника: он
#: датирован, и по нему видно, каким изданием справочника одобрено.
APPROVAL_RULE = "seed_canonical_catalog"


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
            n_cat, n_tpl, n_hc, n_kept = self._seed(rows, source_name=path.name)

        self.stdout.write(self.style.SUCCESS(
            f"Canonical catalog seeded: +{n_cat} categories, +{n_tpl} templates "
            f"(health_check={n_hc}), existing templates left as is: {n_kept}. "
            f"Totals: categories={ServiceCategory.objects.count()}, "
            f"templates={ServiceTemplate.objects.count()}."
        ))

    @staticmethod
    def _preview(rows: list[dict]) -> tuple[int, int, int]:
        cats = {r["category"] for r in rows}
        subs = {r["subcategory"] for r in rows if r.get("subcategory")}
        hc = sum(1 for r in rows if str(r.get("requires_health_check")).lower() == "true")
        return len(cats), len(subs), hc

    def _seed(self, rows: list[dict], *, source_name: str) -> tuple[int, int, int, int]:
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
        kept_templates = 0
        health_check = 0
        for idx, r in enumerate(rows):
            sub_no = r.get("subcategory_no")
            category = sub_by_no[sub_no] if sub_no else top_by_no[r["category_no"]]
            hc = str(r.get("requires_health_check")).lower() == "true"
            health_check += int(hc)
            name = r["service"]
            # get_or_create: defaults — только для НОВОЙ строки. Существующую
            # не трогаем (DRF-2663, шапка модуля): её поля под рукой человека.
            _, created = ServiceTemplate.objects.get_or_create(
                category=category,
                name=name,
                defaults={
                    # Одобрение детерминированным правилом с провенансом
                    # (§76 разрешает такую форму наравне с человеком,
                    # §93 требует её у канона). Правило проверяемое:
                    # строка пришла из эталонного списка владельца, имя
                    # файла — в основании. Без этого вновь заведённый
                    # справочник целиком лёг бы черновым. Ставится один
                    # раз, при заведении: `approved_at` — дата акта
                    # одобрения, а не прогона (DRF-2663).
                    #
                    # `seed_service_templates` (DRF-196) намеренно НЕ
                    # трогаем: его сорок строк — не эталонный список
                    # владельца, и черновое состояние для них верное.
                    "lifecycle": ServiceTemplate.Lifecycle.APPROVED,
                    "approved_by": None,
                    "approved_rule": APPROVAL_RULE,
                    "approval_rule_version": source_name,
                    "approved_at": timezone.now(),
                    "approval_source_ref": f"эталонный справочник владельца: {source_name}",
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
            kept_templates += int(not created)

        return created_categories, created_templates, health_check, kept_templates
