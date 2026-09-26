"""DRF-2429 — новая строка сида доходит до правила связи демо-услуг.

До правки сид заводил строки ``unmapped``, а ``confirm_seeded_links`` (DRF-2408)
берёт только ``review_required``: после нового запуска сида решение владельца
«нормальная связь с каноном» выполнялось наполовину и молча.

Узлы держат:

* **сид → правило → у всех строк сида связь**, ``unmapped`` — ноль, и ноль стоит
  на непустом охвате (число строк названо, а не «больше нуля»);
* **контроль, отличающийся одним признаком**: тот же прогон без шага перехода у
  сида оставляет строки ``unmapped`` и правилу невидимыми — без него первый узел
  прошёл бы и там, где шага нет;
* **сид решает только за свои строки**: строка сида в чужом салоне остаётся
  ``unmapped``, и в демо-салоне ручная строка — тоже, при соседке-близнеце с
  ``source=seed``, которая переведена (пара отличается одним ``source``);
* **принятое решение не сбрасывается**: повторный сид после правила не
  возвращает ``verified`` в очередь;
* **сухой прогон ничего не пишет**, но переход в отчёте виден числом.

``--apply`` здесь — запись в тестовую базу прогона, не в данные стенда.
"""
from __future__ import annotations

import json
import uuid
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command

from services.models import SalonService, ServiceTemplate
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

SEEDS = Path(__file__).resolve().parents[1] / "seeds"
CANONICAL = SEEDS / "canonical_catalog_2026-07.json"
GOAL_OPTIONS = SEEDS / "goal_options_2026-08.json"
DEMO_SALONS = SEEDS / "demo_salons_2026-08.json"

DEMO_SLUGS = tuple(
    s["slug"] for s in json.loads(DEMO_SALONS.read_text(encoding="utf-8"))["salons"]
)
# Столько строк соответствий заводит сид (замер держит и test_seed_demo_salons).
SEEDED_ROWS = 171

Status = SalonService.MappingStatus


@pytest.fixture
def catalog(db):
    """Настоящий канонический каталог: сид ложится на его пары «категория → шаблон»."""
    call_command("seed_canonical_catalog", "--file", str(CANONICAL), verbosity=0)
    call_command("seed_goal_options", "--file", str(GOAL_OPTIONS), verbosity=0)


def _seed(*args) -> str:
    out = StringIO()
    call_command("seed_demo_salons", "--file", str(DEMO_SALONS), *args, stdout=out)
    return out.getvalue()


def _link() -> None:
    call_command("confirm_seeded_links", "--apply", stdout=StringIO())


def _seeded():
    return SalonService.objects.filter(
        tenant__slug__in=DEMO_SLUGS, source=SalonService.Source.SEED,
    )


def test_seed_then_rule_links_every_seeded_row(catalog) -> None:
    _seed("--apply")
    _link()

    rows = _seeded()
    # Охват — первым: без него «ноль unmapped» прошёл бы и на пустой базе.
    assert rows.count() == SEEDED_ROWS
    assert rows.filter(template__isnull=True).count() == 0
    assert rows.filter(mapping_status=Status.UNMAPPED).count() == 0
    assert rows.filter(mapping_status=Status.VERIFIED).count() == SEEDED_ROWS


def test_without_the_seed_step_the_rule_does_not_see_the_rows(catalog) -> None:
    """Контроль: всё то же, кроме шага перехода у сида."""
    noop = {"promoted": 0, "still_unmapped": 0, "examined": 0}
    with mock.patch(
        "services.management.commands.seed_demo_salons.promote_linked_undecided",
        return_value=noop,
    ):
        _seed("--apply")
    _link()

    rows = _seeded()
    assert rows.count() == SEEDED_ROWS
    assert rows.filter(mapping_status=Status.UNMAPPED).count() == SEEDED_ROWS
    assert rows.filter(mapping_status=Status.VERIFIED).count() == 0


def test_the_seed_decides_only_for_its_own_rows(catalog) -> None:
    template = ServiceTemplate.objects.filter(lifecycle="approved").first()
    assert template is not None
    stranger = Tenant.objects.create(
        slug=f"not-demo-{uuid.uuid4().hex[:8]}", name="Чужой", kind=Tenant.Kind.SALON,
    )
    foreign = SalonService.objects.create(
        tenant=stranger,
        template=template,
        category=template.category,
        name="Чужая строка сида",
        duration_minutes=60,
        base_price=1000,
        source=SalonService.Source.SEED,
    )
    assert foreign.mapping_status == Status.UNMAPPED

    _seed("--apply")

    foreign.refresh_from_db()
    assert foreign.mapping_status == Status.UNMAPPED
    # Положительная стража: свои строки в этом же прогоне переведены.
    assert _seeded().filter(mapping_status=Status.REVIEW_REQUIRED).count() == SEEDED_ROWS


def test_in_a_demo_salon_only_the_seed_source_is_promoted(catalog) -> None:
    """Пара строк, различающихся ровно одним признаком — ``source``.

    Салон тот же (демо, из этого прогона), шаблон тот же, статус тот же. Если
    ``source=seed`` выпадет из отбора шага, ручная строка тоже уйдёт в
    ``review_required`` — и узел выше этого не заметит: там ручных строк нет.
    """
    _seed("--apply")
    tenant = Tenant.all_objects.get(slug=DEMO_SLUGS[0])
    template = ServiceTemplate.objects.filter(lifecycle="approved").first()
    assert template is not None

    def _row(source: str) -> SalonService:
        return SalonService.objects.create(
            tenant=tenant,
            template=template,
            category=template.category,
            name=f"Пара по источнику {source}",
            duration_minutes=60,
            base_price=1000,
            source=source,
        )

    seeded = _row(SalonService.Source.SEED)
    manual = _row(SalonService.Source.MANUAL)

    _seed("--apply")

    seeded.refresh_from_db()
    manual.refresh_from_db()
    assert seeded.mapping_status == Status.REVIEW_REQUIRED
    assert manual.mapping_status == Status.UNMAPPED


def test_a_second_seed_does_not_requeue_a_confirmed_link(catalog) -> None:
    _seed("--apply")
    _link()
    assert _seeded().filter(mapping_status=Status.VERIFIED).count() == SEEDED_ROWS

    output = _seed("--apply")

    assert _seeded().filter(mapping_status=Status.VERIFIED).count() == SEEDED_ROWS
    assert f"mapping: examined={SEEDED_ROWS} promoted_to_review_required=0 " in output


def test_dry_run_writes_nothing_but_reports_the_transition(catalog) -> None:
    output = _seed()

    assert _seeded().count() == 0
    assert (
        f"[dry-run] mapping: examined={SEEDED_ROWS} "
        f"promoted_to_review_required={SEEDED_ROWS} still_unmapped=0"
    ) in output
