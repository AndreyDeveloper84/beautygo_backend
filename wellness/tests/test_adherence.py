"""Приёмка DRF-1334 — Plan Adherence по плановому обязательству.

Контракт: docs/DRAFT_ADHERENCE_CONTRACT.md. Три приёмочных случая:
1. факт без планового обязательства не растит adherence ни на сколько;
2. то же действие при наличии обязательства засчитывается;
3. семь записей в один день при per_day за неделю — 1/7, а не 7/7
   (вердикт 25.08: счёт по ведру каденса).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import fields
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from django.utils import timezone

from nutrition.models import FoodLog, WaterEntry
from users.models import User
from wellness.adherence import ActionAdherence, compute_plan_adherence
from wellness.fact_providers import count_facts
from wellness.models import PersonalPlan, PlanAction


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="bot:wellness-adherence", password="x", role="client",
        phone="+79995000033", is_proxy=True,
    )


@pytest.fixture
def plan(db, customer):
    return PersonalPlan.objects.create(user=customer)


def _water(user, *, at):
    return WaterEntry.objects.create(user=user, ts=at, ml=250, water_ml=250.0)


def _food(user, *, at):
    return FoodLog.objects.create(
        user=user, dish_name="Test", meal_type="lunch",
        logged_at=at, idempotency_key=str(uuid4()),
    )


def _at(day: date, hour: int = 10) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour=hour)))


@pytest.mark.django_db
class TestAcceptance:
    def test_fact_without_obligation_grows_nothing(self, customer, plan):
        """Случай 1: вода записана, обязательства нет — adherence пуст.

        Положительная стража на тех же данных: провайдер факт **видит**
        (count_facts == 1), значит пустой ответ — следствие отсутствия
        PlanAction, а не протухшей фикстуры. Пара test_fact_with_obligation_counts
        проходит те же данные через тот же путь с обязательством."""
        today = timezone.localdate()
        _water(customer, at=_at(today))
        assert count_facts("log_water", customer.pk, _at(today).replace(hour=0), _at(today + timedelta(days=1))) == 1
        assert compute_plan_adherence(plan, today, today + timedelta(days=1)) == []

    def test_fact_with_obligation_counts(self, customer, plan):
        """Случай 2: то же действие при наличии PlanAction засчитывается."""
        PlanAction.objects.create(
            plan=plan, action_type=PlanAction.ActionType.LOG_WATER,
            cadence=PlanAction.Cadence.PER_DAY, target_count=1,
        )
        today = timezone.localdate()
        _water(customer, at=_at(today))
        [result] = compute_plan_adherence(plan, today, today + timedelta(days=1))
        assert result.fulfilled_count == 1
        assert result.target_total == 1

    def test_seven_records_one_day_is_one_of_seven(self, customer, plan):
        """Случай 3 (вердикт): 7 записей в один день при per_day за неделю
        — 1/7, а не 7/7. Распределение не стирается."""
        PlanAction.objects.create(
            plan=plan, action_type=PlanAction.ActionType.LOG_WATER,
            cadence=PlanAction.Cadence.PER_DAY, target_count=1,
        )
        today = timezone.localdate()
        start = today - timedelta(days=today.weekday())  # понедельник
        for hour in (6, 8, 10, 12, 14, 16, 18):
            _water(customer, at=_at(start, hour))
        [result] = compute_plan_adherence(plan, start, start + timedelta(days=7))
        assert (result.fulfilled_count, result.target_total) == (1, 7)

    def test_per_week_buckets(self, customer, plan):
        PlanAction.objects.create(
            plan=plan, action_type=PlanAction.ActionType.LOG_FOOD,
            cadence=PlanAction.Cadence.PER_WEEK, target_count=2,
        )
        today = timezone.localdate()
        start = today - timedelta(days=today.weekday())
        _food(customer, at=_at(start))
        _food(customer, at=_at(start + timedelta(days=2)))
        [result] = compute_plan_adherence(plan, start, start + timedelta(days=14))
        assert (result.fulfilled_count, result.target_total) == (2, 4)

    def test_deleted_water_is_not_a_fact(self, customer, plan):
        PlanAction.objects.create(
            plan=plan, action_type=PlanAction.ActionType.LOG_WATER,
            cadence=PlanAction.Cadence.PER_DAY, target_count=1,
        )
        today = timezone.localdate()
        row = _water(customer, at=_at(today))
        row.deleted_at = timezone.now()
        row.save()
        [result] = compute_plan_adherence(plan, today, today + timedelta(days=1))
        assert result.fulfilled_count == 0

    def test_actions_counted_independently(self, customer, plan):
        PlanAction.objects.create(
            plan=plan, action_type=PlanAction.ActionType.LOG_WATER,
            cadence=PlanAction.Cadence.PER_DAY, target_count=1,
        )
        PlanAction.objects.create(
            plan=plan, action_type=PlanAction.ActionType.LOG_FOOD,
            cadence=PlanAction.Cadence.PER_DAY, target_count=3,
        )
        today = timezone.localdate()
        _water(customer, at=_at(today))
        _food(customer, at=_at(today))
        results = compute_plan_adherence(plan, today, today + timedelta(days=1))
        by_type = {r.action_type: r for r in results}
        assert by_type["log_water"].fulfilled_count == 1
        assert by_type["log_food"].fulfilled_count == 1
        assert by_type["log_food"].target_total == 3


@pytest.mark.django_db
class TestProviderContract:
    def test_unknown_action_type_is_programmer_error(self, customer):
        today = timezone.localdate()
        with pytest.raises(ValueError, match="no fact provider"):
            count_facts("visited_massage", customer.pk, _at(today), _at(today, 23))


WELLNESS_DIR = Path(__file__).resolve().parent.parent


class TestBoundary:
    def test_result_has_no_plan_total(self):
        """Ни одного поля, куда сходятся действия плана в одно число."""
        assert {f.name for f in fields(ActionAdherence)} == {
            "action_type", "cadence", "target_total", "fulfilled_count",
        }

    def test_adherence_does_not_import_progress_module(self):
        """Линии не сходятся и в коде: adherence не импортирует
        wellness.progress; журналы читаются, NutritionProfile — никогда."""
        for name in ("adherence.py", "fact_providers.py"):
            tree = ast.parse((WELLNESS_DIR / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert node.module != "wellness.progress"
                    assert not (
                        node.module == "nutrition.models"
                        and any(a.name == "NutritionProfile" for a in node.names)
                    )

    def test_progress_module_stays_clean_after_adherence(self):
        """Регрессионный страж DRF-1343: progress по-прежнему не импортирует
        nutrition даже сверх базовой загрузки приложений."""
        code = (
            "import django, os, sys; "
            "os.environ.setdefault("
            "'DJANGO_SETTINGS_MODULE', 'djangoProject.settings.test'); "
            "django.setup(); "
            "before = {m for m in sys.modules if m.startswith('nutrition')}; "
            "import wellness.progress; "
            "after = {m for m in sys.modules if m.startswith('nutrition')}; "
            "assert after - before == set(), after - before"
        )
        subprocess.run(
            [sys.executable, "-c", code], cwd=WELLNESS_DIR.parent, check=True,
        )
