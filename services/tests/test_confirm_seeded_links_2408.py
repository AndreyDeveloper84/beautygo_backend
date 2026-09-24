"""DRF-2408 — довести связь строк сида: своё правило, свой провенанс.

Решение владельца §77 п.31 (24.09): демонстрационные салоны остаются, и у их
услуг должна быть нормальная связь с каноном. Привязка уже стоит — не хватает
статуса и провенанса.

Узлы держат:

* строка сида получает `VERIFIED` со **своим** именем правила, версией, датой и
  основанием, и `mapping_confirmed_by` пуст: подтверждает правило;
* **провенанс не пустой ни на одной** — проба только по статусу пропустила бы
  главный дефект;
* **чужое правило ни к одной строке не применяется**, в обе стороны: строка
  сида не получает правило выбора мастера, строка выбора — правило сида;
* **защищённый слаг не трогается**: у боевого салона числа до и после равны;
* **повторный прогон ничего не меняет**, включая дату подтверждения;
* строка **без привязки** пропускается и попадает в счётчик — замер «у всех
  привязка есть» сделан не здесь, и исключение должно быть названо числом;
* **сухой прогон — умолчание**: без `--apply` не записывается ничего;
* **ворота резолвера этой работе не мешают** — и это проверено запуском, а не
  рассуждением: рядом стоит отказ тех же ворот на `map_salon_services --apply`.
"""

from __future__ import annotations

import uuid
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from services.management.commands.confirm_seeded_links import (
    PROTECTED_SLUGS,
    SEED_LINK_RULE,
    SEED_LINK_RULE_VERSION,
)
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant

pytestmark = pytest.mark.django_db


def _cat(name: str) -> ServiceCategory:
    return ServiceCategory.objects.get_or_create(name=name)[0]


def _tpl(cat_name: str = "Базовый ручной массаж") -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=_cat(cat_name),
        name=f"Массаж {uuid.uuid4().hex[:6]}",
        name_short="Массаж",
        canonical_code=f"1.1.{uuid.uuid4().int % 900 + 10}",
        lifecycle="approved",
        requires_health_check=False,
        approved_rule="test",
        approval_rule_version="1",
        approved_at=timezone.now(),
        approval_source_ref="fixture",
    )


def _tenant(slug: str | None = None) -> Tenant:
    # Защищённый слаг может быть уже заведён общей фикстурой репозитория —
    # берём существующего, иначе узел падал бы на уникальности, а не на предмете.
    if slug:
        tenant, _ = Tenant.objects.get_or_create(
            slug=slug, defaults={"name": "Боевой", "kind": Tenant.Kind.SALON}
        )
        return tenant
    return Tenant.objects.create(
        slug=f"demo-{uuid.uuid4().hex[:8]}", name="Демо", kind=Tenant.Kind.SALON
    )


def _row(tenant: Tenant, **over) -> SalonService:
    template = over.pop("template", None) or _tpl()
    fields = {
        "tenant": tenant,
        "template": template,
        "category": template.category,
        "name": f"{template.name} {uuid.uuid4().hex[:4]}",
        "duration_minutes": 60,
        "source": SalonService.Source.SEED,
        "mapping_status": SalonService.MappingStatus.REVIEW_REQUIRED,
    }
    fields.update(over)
    return SalonService.objects.create(**fields)


def _run(**options) -> str:
    out = StringIO()
    call_command("confirm_seeded_links", stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


class TestTheLinkIsFinishedWithItsOwnRule:
    def test_provenance_is_complete_on_every_row(self) -> None:
        """Проба только по статусу пропустила бы главный дефект.

        «Подтверждено» без подтвердившего через месяц читается как умолчание —
        ровно то, из-за чего литерал однажды стал «проверенным фактом».
        """
        tenant = _tenant()
        rows = [_row(tenant) for _ in range(3)]

        _run(apply=True)

        for row in rows:
            row.refresh_from_db()
            assert row.mapping_status == SalonService.MappingStatus.VERIFIED
            assert row.mapping_confirmed_rule == SEED_LINK_RULE
            assert row.mapping_rule_version == SEED_LINK_RULE_VERSION
            assert row.mapping_confirmed_at is not None
            assert row.mapping_source_ref != ""
            # Подтверждает правило, не человек.
            assert row.mapping_confirmed_by_id is None

    def test_the_rule_name_says_where_the_link_came_from(self) -> None:
        """Имя называет происхождение, а не заслугу.

        «Проверено» здесь было бы неправдой: никто ничего не проверял, связь
        пришла из демонстрационного набора. Через год провенанс должен читаться
        именно так.
        """
        assert SEED_LINK_RULE == "seeded_demo_catalog_link"
        for overclaim in ("verified", "checked", "approved", "human", "master"):
            assert overclaim not in SEED_LINK_RULE


class TestNoRuleIsAppliedToSomeoneElsesRows:
    def test_a_master_selection_does_not_get_the_seed_rule(self) -> None:
        """Строка выбора мастера — чужая для этого правила."""
        tenant = _tenant()
        selected = _row(
            tenant,
            source=SalonService.Source.MANUAL,
            mapping_source_ref=f"master_select:{uuid.uuid4()}",
        )
        mine = _row(tenant)

        _run(apply=True)

        selected.refresh_from_db()
        mine.refresh_from_db()
        # Положительная пара в том же прогоне: своя строка подтверждена.
        assert mine.mapping_status == SalonService.MappingStatus.VERIFIED
        assert selected.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
        assert selected.mapping_confirmed_rule == ""

    def test_a_seed_row_does_not_carry_the_master_select_rule(self) -> None:
        """Обратная сторона: правило сида и правило выбора — разные имена.

        Если бы они совпали, провенанс сказал бы «мастер выбрал» о строке, где
        мастер не выбирал.
        """
        from services.offer_selection import MASTER_SELECT_RULE

        tenant = _tenant()
        row = _row(tenant)

        _run(apply=True)

        row.refresh_from_db()
        assert row.mapping_confirmed_rule != MASTER_SELECT_RULE


class TestTheProtectedSalonIsUntouched:
    def test_counts_before_and_after_are_equal(self) -> None:
        """У боевого салона настоящие записи настоящих людей."""
        live = _tenant(slug=next(iter(PROTECTED_SLUGS)))
        demo = _tenant()
        live_row = _row(live)
        demo_row = _row(demo)

        _run(apply=True)

        live_row.refresh_from_db()
        demo_row.refresh_from_db()
        # Положительная пара: демонстрационная строка подтверждена в том же прогоне.
        assert demo_row.mapping_status == SalonService.MappingStatus.VERIFIED
        assert live_row.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
        assert live_row.mapping_confirmed_rule == ""

    def test_asking_for_the_protected_slug_is_refused(self) -> None:
        with pytest.raises(CommandError) as exc:
            _run(tenant=next(iter(PROTECTED_SLUGS)), apply=True)

        assert "защищён" in str(exc.value)


class TestARowWithoutALinkIsCountedNotConfirmed:
    def test_it_is_skipped_and_named_by_number(self) -> None:
        """Замер «у всех привязка есть» сделан не здесь.

        Если на стенде найдётся исключение, оно обязано быть **названо числом**,
        а не пройти молча: иначе чужой замер становится посылкой, которую никто
        не проверял.
        """
        tenant = _tenant()
        unlinked = _row(tenant)
        SalonService.objects.filter(pk=unlinked.pk).update(template=None)
        linked = _row(tenant)

        report = _run(apply=True)

        unlinked.refresh_from_db()
        linked.refresh_from_db()
        assert linked.mapping_status == SalonService.MappingStatus.VERIFIED
        assert unlinked.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
        assert "без привязки (пропущено): 1" in report


class TestDryRunIsTheDefault:
    def test_without_apply_nothing_is_written(self) -> None:
        tenant = _tenant()
        row = _row(tenant)

        report = _run()

        row.refresh_from_db()
        assert row.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
        assert "сухой прогон" in report
        assert "с привязкой (к записи):   1" in report


class TestTheRunIsIdempotent:
    def test_a_second_run_changes_nothing_including_the_date(self) -> None:
        """Иначе «когда подтвердили» станет «когда последний раз запускали»."""
        tenant = _tenant()
        row = _row(tenant)

        _run(apply=True)
        row.refresh_from_db()
        first = row.mapping_confirmed_at

        report = _run(apply=True)
        row.refresh_from_db()

        assert row.mapping_confirmed_at == first
        assert "с привязкой (к записи):   0" in report


class TestTheResolverGateIsNotInTheWay:
    def test_the_gate_refuses_the_resolver_but_not_this_command(self) -> None:
        """Проверено запуском, а не рассуждением.

        Ворота `authorize_apply` охраняют запись **решений резолвера**
        (MAP-AUTO-06): выбор кандидата, план провенанса, правила R0/R1/R2. Здесь
        кандидата не ищут — привязка уже стоит. Узел ставит рядом обе стороны:
        резолвер отказывают, эта команда пишет.
        """
        tenant = _tenant()
        row = _row(tenant)

        # Та же операция через резолвер — отказ ворот.
        with pytest.raises(CommandError) as exc:
            call_command(
                "map_salon_services",
                tenant=tenant.slug,
                apply=True,
                stdout=StringIO(),
                stderr=StringIO(),
            )
        assert "OD-NEW-7" in str(exc.value)

        # Эта команда — пишет.
        _run(apply=True)

        row.refresh_from_db()
        assert row.mapping_status == SalonService.MappingStatus.VERIFIED
