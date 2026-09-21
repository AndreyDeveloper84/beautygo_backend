"""Миграция-заполнитель шаблонов §51 (DRF-2229) — заполняет пустоту, админку не трогает.

Plan Lite на стенде выключен, пока наличие шаблонов §51 не гарантировано.
Миграция ``wellness.0007`` кладёт версию 1 для цели, у которой нет ни одной
строки, и больше ничего.

* m1 — пустая база → 7 активных версий 1, содержимое — ровно таблица кода;
* m2 — повтор → без изменений (ни строк, ни версий);
* m3 — правка в админке (активная версия отличается от кода) → миграция её
  не трогает: активна, версия прежняя, новой нет;
* m4 — у цели только снятые версии (сняли в админке) → миграция её не
  возрождает: это решение, а не пустота;
* m5 — частично заполненная база → докладываются только недостающие цели;
* m6 — ``seed_plan_templates`` после миграции: на нетронутых шаблонах —
  ``unchanged=7``; на правленной в админке — КЛАДЁТ ВЕРСИЮ КОДА ПОВЕРХ (снимает
  админскую). Это прежнее поведение команды, миграцией не изменённое —
  узел его называет, а не одобряет (источник истины решает владелец);
* m7 — миграция в графе: тестовая база собрана с ней — 7 активных до любого
  теста; обратная — no-op.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as django_apps

from wellness import plan_lite_templates
from wellness.models import PlanTemplate

pytestmark = pytest.mark.django_db

_MIGRATION = importlib.import_module("wellness.migrations.0007_seed_plan_templates_if_empty")
_GOALS = [row["goal_key"] for row in plan_lite_templates.PLAN_TEMPLATES_SEED]


def _run() -> None:
    _MIGRATION.seed_missing_goals(django_apps, None)


def _snapshot() -> list[tuple]:
    return sorted(
        PlanTemplate.objects.values_list("goal_key", "version", "is_active", "why_text")
    )


class TestM1EmptyDatabase:
    def test_seven_active_versions_equal_to_the_code_table(self) -> None:
        PlanTemplate.objects.all().delete()
        _run()
        active = {t.goal_key: t for t in PlanTemplate.objects.filter(is_active=True)}
        assert sorted(active) == sorted(_GOALS) and len(active) == 7
        for row in plan_lite_templates.PLAN_TEMPLATES_SEED:
            t = active[row["goal_key"]]
            assert t.version == 1
            assert t.actions == row["actions"]
            assert t.why_text == row["why_text"]
            assert t.nutrition_goal_hint == list(row["nutrition_goal_hint"])


class TestM2Repeat:
    def test_second_run_changes_nothing(self) -> None:
        PlanTemplate.objects.all().delete()
        _run()
        before = _snapshot()
        _run()
        assert _snapshot() == before
        assert PlanTemplate.objects.count() == 7


class TestM3AdminEditIsUntouched:
    def test_edited_active_version_stays_active_and_alone(self) -> None:
        PlanTemplate.objects.all().delete()
        _run()
        edited = PlanTemplate.objects.get(goal_key="body_shape", is_active=True)
        edited.why_text = "Правка владельца в админке"
        edited.save(update_fields=["why_text"])
        _run()
        rows = list(PlanTemplate.objects.filter(goal_key="body_shape"))
        assert len(rows) == 1
        assert rows[0].is_active and rows[0].why_text == "Правка владельца в админке"


class TestM4DeactivatedByAdminIsNotRevived:
    def test_goal_with_only_inactive_versions_is_left_as_is(self) -> None:
        PlanTemplate.objects.all().delete()
        _run()
        PlanTemplate.objects.filter(goal_key="relax").update(is_active=False)
        _run()
        relax = list(PlanTemplate.objects.filter(goal_key="relax"))
        assert len(relax) == 1 and not relax[0].is_active
        # Присутствие рядом: остальные цели активны.
        assert PlanTemplate.objects.filter(is_active=True).count() == 6


class TestM5PartialDatabase:
    def test_only_missing_goals_are_filled(self) -> None:
        PlanTemplate.objects.all().delete()
        seed_row = plan_lite_templates.PLAN_TEMPLATES_SEED[0]
        PlanTemplate.objects.create(
            goal_key=seed_row["goal_key"],
            actions=seed_row["actions"],
            why_text="своё",
            nutrition_goal_hint=[],
            version=3,
            is_active=True,
        )
        _run()
        assert PlanTemplate.objects.filter(is_active=True).count() == 7
        first = PlanTemplate.objects.get(goal_key=seed_row["goal_key"])
        assert first.version == 3 and first.why_text == "своё"


class TestM6SeedCommandAfterTheMigration:
    def test_untouched_templates_are_unchanged_for_the_seed(self) -> None:
        PlanTemplate.objects.all().delete()
        _run()
        report = plan_lite_templates.seed_plan_templates()
        assert (report.created, report.deactivated, report.unchanged) == (0, 0, 7)

    def test_seed_still_lays_the_code_version_over_an_admin_edit(self) -> None:
        """Прежнее поведение команды — названо, не одобрено (решает владелец)."""
        PlanTemplate.objects.all().delete()
        _run()
        edited = PlanTemplate.objects.get(goal_key="body_shape", is_active=True)
        edited.why_text = "Правка владельца в админке"
        edited.save(update_fields=["why_text"])
        report = plan_lite_templates.seed_plan_templates()
        assert (report.created, report.deactivated) == (1, 1)
        active = PlanTemplate.objects.get(goal_key="body_shape", is_active=True)
        assert active.version == 2 and active.why_text != "Правка владельца в админке"


class TestM7InTheGraph:
    def test_the_test_database_was_built_with_the_migration(self) -> None:
        assert sorted(
            PlanTemplate.objects.filter(is_active=True).values_list("goal_key", flat=True)
        ) == sorted(_GOALS)

    def test_reverse_is_a_noop(self) -> None:
        from django.db import migrations

        op = _MIGRATION.Migration.operations[0]
        assert op.reverse_code is migrations.RunPython.noop
