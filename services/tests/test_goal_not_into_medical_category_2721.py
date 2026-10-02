"""DRF-2721 — a goal chip does not lead into a category that is medical through and through.

The goal seed linked ``recharge`` («Восстановить силы») to the whole canon
category 19 «Медико-эстетические и смежные услуги». Every one of its 20
services carries ``requires_health_check`` («Удаление новообразований»,
«Обработка диабетической стопы»…), so the chip recommended medical services
as a way to recharge. The booking gate still refuses a flagged service — the
harm was the recommendation, not an unscreened booking.

Three things are held here, each with the control that keeps it honest:

* f — the seed FILES: no goal is linked to a category or subcategory whose
  canonical services are all flagged. Control: category 19 is such a category
  (20 of 20), and the same check does find a link to it when one is put back;
* m — the data migration removes exactly ``recharge`` × that category: the
  goal's other link, another goal's link to the same category and unrelated
  links survive; a second run changes nothing;
* r — what a reader of goals gets: the medical category answers with no goal,
  ``recharge`` still answers for the massage branch, another goal still
  answers for its own category.

The services of category 19 are not touched anywhere — only the goal link.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command

from services.goal_resolution import build_category_goal_index
from services.models import GoalOption, GoalOptionCategory, ServiceCategory

SEEDS = Path(__file__).resolve().parents[1] / "seeds"
GOALS_FILE = SEEDS / "goal_options_2026-08.json"
CANON_FILE = SEEDS / "canonical_catalog_2026-07.json"

MEDICAL = "Медико-эстетические и смежные услуги"
MASSAGE_BRANCH = "Оздоровительные и восстановительные массажи"
FACE = "Косметология лица"

migration = importlib.import_module(
    "services.migrations.0031_drf2721_unlink_recharge_from_medical_category"
)


def _goals() -> list[dict]:
    return json.loads(GOALS_FILE.read_text(encoding="utf-8"))


def _canon() -> list[dict]:
    return json.loads(CANON_FILE.read_text(encoding="utf-8"))


def _flagged(row: dict) -> bool:
    return str(row["requires_health_check"]).lower() == "true"


def _links_into_all_flagged(goals: list[dict], canon: list[dict]) -> list[tuple[str, str]]:
    """(goal key, name) for every link whose category or subcategory holds
    canonical services that are ALL under the health-check flag."""
    out = []
    for goal in goals:
        for name in goal["categories"]:
            members = [r for r in canon if name in (r["category"], r["subcategory"])]
            if members and all(_flagged(r) for r in members):
                out.append((goal["key"], name))
    return out


# ── f: the seed files ────────────────────────────────────────────────────────


class TestFTheSeedFiles:
    def test_the_medical_category_is_flagged_through_and_through(self):
        """What the guard is about, by literal numbers: 20 services, 20 flagged."""
        members = [r for r in _canon() if r["category"] == MEDICAL]

        assert len(members) == 20
        assert sum(map(_flagged, members)) == 20

    def test_no_goal_leads_into_an_all_flagged_category(self):
        goals = _goals()
        # Presence first: the file is read and the check has links to look at.
        assert len(goals) == 7
        assert sum(len(g["categories"]) for g in goals) == 19

        assert _links_into_all_flagged(goals, _canon()) == []

    def test_recharge_keeps_only_the_massage_branch(self):
        (recharge,) = [g for g in _goals() if g["key"] == "recharge"]

        assert recharge["categories"] == [MASSAGE_BRANCH]

    def test_the_check_finds_the_link_when_it_is_put_back(self):
        """The guard is not blind: the defect, restored, is named."""
        goals = _goals()
        for goal in goals:
            if goal["key"] == "recharge":
                goal["categories"] = [MEDICAL, *goal["categories"]]

        assert _links_into_all_flagged(goals, _canon()) == [("recharge", MEDICAL)]


# ── m, r: the rows on the stand ──────────────────────────────────────────────


@pytest.fixture
def stand(db) -> dict:
    """The stand before the fix: recharge linked to the medical category and to
    the massage branch; another goal linked to its own category; and one link
    a curator added by hand — another goal on the medical category."""
    massage_root = ServiceCategory.objects.create(name="Массаж тела")
    cats = {
        MEDICAL: ServiceCategory.objects.create(name=MEDICAL),
        MASSAGE_BRANCH: ServiceCategory.objects.create(name=MASSAGE_BRANCH, parent=massage_root),
        FACE: ServiceCategory.objects.create(name=FACE),
    }
    recharge = GoalOption.objects.create(key="recharge", label="Восстановить силы", sort_order=70)
    skin = GoalOption.objects.create(key="skin_care", label="Позаботиться о коже лица", sort_order=60)
    GoalOptionCategory.objects.create(goal_option=recharge, category=cats[MEDICAL])
    GoalOptionCategory.objects.create(goal_option=recharge, category=cats[MASSAGE_BRANCH])
    GoalOptionCategory.objects.create(goal_option=skin, category=cats[FACE])
    GoalOptionCategory.objects.create(goal_option=skin, category=cats[MEDICAL])
    return cats


def _pairs() -> set[tuple[str, str]]:
    return set(GoalOptionCategory.objects.values_list("goal_option__key", "category__name"))


def _keys(category: ServiceCategory) -> list[str]:
    return [g["key"] for g in build_category_goal_index().goals_for_category(category.id)]


def _migrate() -> None:
    migration.unlink_recharge_from_medical_category(django_apps, None)


class TestMTheMigration:
    def test_it_removes_exactly_one_pair(self, stand):
        assert ("recharge", MEDICAL) in _pairs()

        _migrate()

        assert _pairs() == {
            ("recharge", MASSAGE_BRANCH),
            ("skin_care", FACE),
            ("skin_care", MEDICAL),
        }

    def test_a_second_run_changes_nothing(self, stand):
        _migrate()
        after_first = _pairs()

        _migrate()

        assert _pairs() == after_first
        assert len(after_first) == 3

    def test_the_services_and_the_category_stay(self, stand):
        _migrate()

        assert ServiceCategory.objects.filter(name=MEDICAL).count() == 1

    def test_it_names_the_pair_by_literals(self):
        """The pair is the decision; it is not derived from anything."""
        assert (migration.GOAL_KEY, migration.CATEGORY_NAME) == ("recharge", MEDICAL)


class TestRWhatAReaderGets:
    def test_before_the_fix_the_medical_category_answers_recharge(self, stand):
        assert _keys(stand[MEDICAL]) == ["skin_care", "recharge"]

    def test_after_it_recharge_is_gone_from_the_medical_category(self, stand):
        _migrate()

        assert _keys(stand[MEDICAL]) == ["skin_care"]

    def test_recharge_still_answers_for_the_massage_branch(self, stand):
        _migrate()

        assert _keys(stand[MASSAGE_BRANCH]) == ["recharge"]

    def test_another_goal_still_answers_for_its_category(self, stand):
        _migrate()

        assert _keys(stand[FACE]) == ["skin_care"]


class TestTheSeedDoesNotBringItBack:
    @pytest.fixture
    def named_categories(self, stand) -> None:
        """Every category the real seed file names exists, as after the canon seed."""
        have = set(ServiceCategory.objects.values_list("name", flat=True))
        for goal in _goals():
            for name in goal["categories"]:
                if name not in have:
                    ServiceCategory.objects.create(name=name)
                    have.add(name)

    def test_reseeding_after_the_migration_leaves_the_pair_absent(self, named_categories):
        _migrate()

        call_command("seed_goal_options")

        pairs = _pairs()
        # Presence first: the seed did run and linked recharge where the file says.
        assert ("recharge", MASSAGE_BRANCH) in pairs
        assert ("recharge", MEDICAL) not in pairs

    def test_the_seed_alone_would_not_have_removed_it(self, named_categories):
        """Why the migration exists: without ``--prune`` the seed keeps the row."""
        call_command("seed_goal_options")

        assert ("recharge", MEDICAL) in _pairs()
