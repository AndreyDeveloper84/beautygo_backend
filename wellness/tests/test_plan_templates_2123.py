"""План-A: шаблоны Plan Lite по цели — данные, не код (DRF-2123, §51).

Решение владельца 19.09: таблица «цель → 1–3 обязательства + почему» —
курируемые ДАННЫЕ (``wellness.PlanTemplate``, версионируются, правятся в
админке), сид ``seed_plan_templates`` кладёт таблицу §51 ДОСЛОВНО и
идемпотентен; предложение плана — ``GET plan-lite/proposal/`` по активной
цели вызывающего, план НЕ создаёт; ``POST plan-lite/`` принимает
``template_version`` и помечает ``PersonalPlan.source``.

Новый каденс ``per_2_weeks``: ведро — 14 дней ОТ СОЗДАНИЯ ПЛАНА (текущее
ведро содержит «сейчас»), в отличие от недели пн–вс.

Сторожа, каждый с ложным входом:

* **снимок таблицы §51** — сид сверяется с дословной копией таблицы в
  тесте (две копии: код и тест; расхождение — красный);
* **идемпотентность** — повтор → 0 новых; подмена текста в источнике →
  новая версия, старая деактивирована, активная по цели ровно одна;
* **реестр стирания** — ``PlanTemplate`` не указывает на ``User``,
  ``undecided_pointers()`` пуст; перепись импортов plan_lite* (DRF-2101)
  накрывает и ``plan_lite_templates.py``.

Красное до правки объявлено поимённо до прогона (модели, команды,
маршрута, каденса, поля нет).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from io import StringIO

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from goals.models import ClientGoal
from nutrition.models import WaterEntry
from users.models import User
from wellness.models import PersonalPlan, PlanAction

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-ayla-internal-token-plan-templates"
PLAN_URL = "/api/v1/internal/me/plan-lite/"
PROPOSAL_URL = "/api/v1/internal/me/plan-lite/proposal/"
OWNER = "bot:plan-templates-owner"

#: Таблица владельца §51 — ДОСЛОВНО (снимок в тесте, вторая копия — в коде).
#: (goal_key, book_service cadence, log_food per_week, log_water per_day, why_text)
OWNER_TABLE_51: list[tuple[str, str, int | None, int | None, str]] = [
    (
        "body_shape", "per_week", 5, 6,
        "Фигура — это регулярность: процедура раз в неделю и еда, которую видно. "
        "Не обещаю килограммы — покажу, что получается",
    ),
    (
        "event", "per_week", None, 5,
        "К дате лучше идти малыми шагами: процедура заранее, а не накануне, "
        "и вода — кожа скажет спасибо",
    ),
    (
        "new_look", "per_week", None, None,
        "Образ — это несколько визитов, не один. Начнём с первого, дальше подскажу по записям",
    ),
    (
        "recharge", "per_week", 3, 5,
        "Силы возвращают сон, вода и еда без пропусков — и час на себя раз в неделю",
    ),
    (
        "relax", "per_week", None, 4,
        "Расслабление не копится про запас — раз в неделю час без телефона",
    ),
    (
        "self_care", "per_2_weeks", 3, 5,
        "Забота — это привычка, не рывок: маленькие регулярные шаги",
    ),
    (
        "skin_care", "per_2_weeks", None, 6,
        "Кожа любит воду и регулярность процедур",
    ),
]


def _expected_actions(row: tuple) -> list[dict]:
    _, book_cadence, food, water, _ = row
    actions = [{"action_type": "book_service", "cadence": book_cadence, "target_count": 1}]
    if food is not None:
        actions.append({"action_type": "log_food", "cadence": "per_week", "target_count": food})
    if water is not None:
        actions.append({"action_type": "log_water", "cadence": "per_day", "target_count": water})
    return actions


@pytest.fixture(autouse=True)
def _token_and_flag(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_LITE_ENABLED = True


@pytest.fixture
def owner(db) -> User:
    return User.objects.create_user(
        username=OWNER, password="x", role="client", phone="+79995002123", is_proxy=True,
    )


@pytest.fixture
def goal(owner) -> ClientGoal:
    return ClientGoal.objects.create(client=owner, goal_key="self_care", source_channel="miniapp")


@pytest.fixture
def seeded(db):
    out = StringIO()
    call_command("seed_plan_templates", stdout=out)
    return out.getvalue()


def _api(external_user_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _at(day: date, hour: int = 10) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour=hour)))


# ─── 1. каденс per_2_weeks ───────────────────────────────────────────────────


class TestPer2WeeksCadence:
    def test_cadence_choice_exists_and_is_accepted_by_the_writer(self, owner, goal) -> None:
        assert PlanAction.Cadence.PER_2_WEEKS == "per_2_weeks"
        resp = _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "book_service", "cadence": "per_2_weeks", "target_count": 1}]},
            format="json",
        )
        assert resp.status_code == 201, resp.content[:400]
        assert PlanAction.objects.get(plan__user=owner).cadence == "per_2_weeks"

    @pytest.mark.parametrize(
        "anchor, today, expected",
        [
            (date(2026, 9, 1), date(2026, 9, 1), (date(2026, 9, 1), date(2026, 9, 15))),
            (date(2026, 9, 1), date(2026, 9, 14), (date(2026, 9, 1), date(2026, 9, 15))),
            (date(2026, 9, 1), date(2026, 9, 15), (date(2026, 9, 15), date(2026, 9, 29))),
            (date(2026, 9, 1), date(2026, 10, 20), (date(2026, 10, 13), date(2026, 10, 27))),
        ],
        ids=["day0", "day13-last-of-bucket", "day14-first-of-next", "day49-fourth-bucket"],
    )
    def test_current_bucket_is_14_days_from_plan_creation(self, anchor, today, expected) -> None:
        from wellness.plan_lite import _current_bucket

        start, end = _current_bucket(PlanAction.Cadence.PER_2_WEEKS, today, anchor=anchor)
        assert (start, end) == expected
        assert start <= today < end  # текущее ведро содержит «сейчас»

    def test_current_bucket_per_week_is_unchanged_by_the_anchor(self) -> None:
        from wellness.plan_lite import _current_bucket

        wed = date(2026, 9, 16)
        assert _current_bucket(PlanAction.Cadence.PER_WEEK, wed, anchor=date(2026, 9, 3)) == (
            date(2026, 9, 14), date(2026, 9, 21),
        )
        assert _current_bucket(PlanAction.Cadence.PER_DAY, wed, anchor=date(2026, 9, 3)) == (
            wed, date(2026, 9, 17),
        )

    def test_payload_done_count_crosses_the_14_day_boundary(self, owner, goal) -> None:
        """Факты на 13-й и 14-й день от создания — в РАЗНЫХ вёдрах: на 14-й
        день done_count считает только 14-й, на 13-й — только 13-й."""
        from wellness.plan_lite import plan_lite_payload

        resp = _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "log_water", "cadence": "per_2_weeks", "target_count": 3}]},
            format="json",
        )
        assert resp.status_code == 201, resp.content[:400]
        plan = PersonalPlan.objects.get(user=owner)
        anchor = timezone.localdate(plan.created_at)
        for day_offset in (0, 13, 14):
            WaterEntry.objects.create(
                user=owner, ts=_at(anchor + timedelta(days=day_offset)), ml=250, water_ml=250.0,
            )

        on_day13 = plan_lite_payload(owner, today=anchor + timedelta(days=13))["actions"][0]
        on_day14 = plan_lite_payload(owner, today=anchor + timedelta(days=14))["actions"][0]

        assert on_day13["bucket"] == {
            "start": anchor.isoformat(), "end": (anchor + timedelta(days=14)).isoformat(),
        }
        assert on_day13["done_count"] == 2  # день 0 и день 13
        assert on_day14["bucket"] == {
            "start": (anchor + timedelta(days=14)).isoformat(),
            "end": (anchor + timedelta(days=28)).isoformat(),
        }
        assert on_day14["done_count"] == 1  # только день 14

    def test_adherence_buckets_per_2_weeks_are_14_days(self, owner) -> None:
        """compute_plan_adherence: 28 дней → два ведра по 14; факты на 13-й
        и 14-й день попадают в разные вёдра (2 из 2 при target 1)."""
        from wellness.adherence import compute_plan_adherence

        plan = PersonalPlan.objects.create(user=owner)
        PlanAction.objects.create(
            plan=plan, action_type=PlanAction.ActionType.LOG_WATER,
            cadence=PlanAction.Cadence.PER_2_WEEKS, target_count=1,
        )
        start = timezone.localdate() - timedelta(days=28)
        for day_offset in (13, 14):
            WaterEntry.objects.create(
                user=owner, ts=_at(start + timedelta(days=day_offset)), ml=250, water_ml=250.0,
            )

        [row] = compute_plan_adherence(plan, start, start + timedelta(days=28))

        assert (row.fulfilled_count, row.target_total) == (2, 2)
        # Ложный вход: те же факты в одном ведре — 1 из 1, не 2.
        [row_one] = compute_plan_adherence(plan, start, start + timedelta(days=14))
        assert (row_one.fulfilled_count, row_one.target_total) == (1, 1)

    def test_fact_provider_counts_across_the_bucket_boundary(self, owner) -> None:
        from wellness.fact_providers import count_fact_days, count_facts

        start = date(2026, 9, 1)
        for day_offset in (13, 14):
            WaterEntry.objects.create(
                user=owner, ts=_at(start + timedelta(days=day_offset)), ml=250, water_ml=250.0,
            )
        first = (_at(start).replace(hour=0), _at(start + timedelta(days=14)).replace(hour=0))
        second = (first[1], _at(start + timedelta(days=28)).replace(hour=0))
        assert count_facts("log_water", owner.pk, *first) == 1
        assert count_facts("log_water", owner.pk, *second) == 1
        assert count_fact_days("log_water", owner.pk, *first) == 1


# ─── 2. таблица владельца — данные + сид ─────────────────────────────────────


class TestSeedMatchesOwnerTable:
    def test_seed_creates_exactly_the_table_51_verbatim(self, seeded) -> None:
        from wellness.models import PlanTemplate

        rows = {t.goal_key: t for t in PlanTemplate.objects.filter(is_active=True)}
        assert sorted(rows) == sorted(r[0] for r in OWNER_TABLE_51)
        for row in OWNER_TABLE_51:
            goal_key, _, _, _, why_text = row
            t = rows[goal_key]
            assert t.why_text == why_text, goal_key
            assert t.actions == _expected_actions(row), goal_key
            assert t.version == 1
        assert PlanTemplate.objects.count() == len(OWNER_TABLE_51)

    def test_code_side_seed_constant_equals_the_snapshot(self) -> None:
        """Две копии таблицы — код и тест — обязаны совпадать дословно."""
        from wellness.plan_lite_templates import PLAN_TEMPLATES_SEED

        as_dict = {r[0]: (r[4], _expected_actions(r)) for r in OWNER_TABLE_51}
        seed = {row["goal_key"]: (row["why_text"], row["actions"]) for row in PLAN_TEMPLATES_SEED}
        assert seed == as_dict

    def test_every_seeded_action_is_within_the_writer_limits(self) -> None:
        """Шаблон должен проходить тот же parse_actions, что и ручной POST."""
        from wellness.plan_lite import parse_actions
        from wellness.plan_lite_templates import PLAN_TEMPLATES_SEED

        for row in PLAN_TEMPLATES_SEED:
            specs = parse_actions(row["actions"])
            assert 1 <= len(specs) <= 3, row["goal_key"]

    def test_seed_is_idempotent(self, seeded) -> None:
        from wellness.models import PlanTemplate

        before = PlanTemplate.objects.count()
        out = StringIO()
        call_command("seed_plan_templates", stdout=out)

        assert PlanTemplate.objects.count() == before
        assert "created=0" in out.getvalue(), out.getvalue()

    def test_changed_text_creates_a_new_version_and_deactivates_the_old(
        self, seeded, monkeypatch,
    ) -> None:
        """Ложный вход: подменить текст одной строки источника — сид обязан
        положить v2 и снять v1, остальные цели не трогать."""
        from wellness import plan_lite_templates as mod
        from wellness.models import PlanTemplate

        changed = [dict(r) for r in mod.PLAN_TEMPLATES_SEED]
        target = next(r for r in changed if r["goal_key"] == "relax")
        target["why_text"] = "Другой текст владельца"
        monkeypatch.setattr(mod, "PLAN_TEMPLATES_SEED", changed)

        out = StringIO()
        call_command("seed_plan_templates", stdout=out)

        versions = list(PlanTemplate.objects.filter(goal_key="relax").order_by("version"))
        assert [(t.version, t.is_active) for t in versions] == [(1, False), (2, True)]
        assert versions[1].why_text == "Другой текст владельца"
        assert versions[1].actions == versions[0].actions
        assert "created=1" in out.getvalue(), out.getvalue()
        # По-прежнему ровно одна активная на каждую цель, других версий нет.
        assert PlanTemplate.objects.filter(is_active=True).count() == len(OWNER_TABLE_51)
        assert PlanTemplate.objects.exclude(goal_key="relax").filter(version__gt=1).count() == 0

    def test_two_active_templates_for_one_goal_are_refused_by_the_schema(self, seeded) -> None:
        from django.db import IntegrityError, transaction

        from wellness.models import PlanTemplate

        with pytest.raises(IntegrityError), transaction.atomic():
            PlanTemplate.objects.create(
                goal_key="relax", version=99, actions=[], why_text="x", is_active=True,
            )

    def test_plan_template_is_registered_in_admin(self) -> None:
        from wellness.models import PlanTemplate

        assert PlanTemplate in admin.site._registry


# ─── 3. предложение плана — GET proposal/ ────────────────────────────────────


class TestProposal:
    def test_proposal_follows_the_callers_active_goal_and_creates_nothing(
        self, seeded, owner, goal,
    ) -> None:
        resp = _api().get(PROPOSAL_URL)

        assert resp.status_code == 200, resp.content[:400]
        body = resp.json()["data"]
        row = next(r for r in OWNER_TABLE_51 if r[0] == "self_care")
        assert body == {
            "goal_key": "self_care",
            "why": row[4],
            "template_version": 1,
            "actions": _expected_actions(row),
        }
        assert PersonalPlan.objects.count() == 0  # предложение — не план

    def test_proposal_without_an_active_goal_is_404_no_active_goal(self, seeded, owner) -> None:
        resp = _api().get(PROPOSAL_URL)

        assert resp.status_code == 404, resp.content[:400]
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        assert resp.json()["error"]["details"]["reason"] == "no_active_goal"

    def test_proposal_without_a_template_is_404_no_template_not_an_empty_plan(
        self, owner, goal,
    ) -> None:
        """Сид не запускался — шаблона нет; ответ 404, не пустой список."""
        resp = _api().get(PROPOSAL_URL)

        assert resp.status_code == 404, resp.content[:400]
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        assert resp.json()["error"]["details"]["reason"] == "no_template"

    def test_proposal_ignores_a_deactivated_version(self, seeded, owner, goal) -> None:
        from wellness.models import PlanTemplate

        PlanTemplate.objects.filter(goal_key="self_care").update(is_active=False)

        resp = _api().get(PROPOSAL_URL)

        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["reason"] == "no_template"

    def test_proposal_flag_off_is_404_plan_lite_disabled_not_5xx(
        self, seeded, owner, goal, settings,
    ) -> None:
        settings.PLAN_LITE_ENABLED = False

        resp = _api().get(PROPOSAL_URL)

        assert resp.status_code == 404, resp.content[:400]
        assert resp.json()["error"]["code"] == "PLAN_LITE_DISABLED"

    def test_proposal_requires_the_service_token(self, seeded, owner, goal) -> None:
        c = APIClient()
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = OWNER
        assert c.get(PROPOSAL_URL).status_code in (401, 403)


# ─── 4. POST plan-lite/ с template_version → PersonalPlan.source ─────────────


class TestPlanSource:
    def test_manual_post_has_source_manual(self, owner, goal) -> None:
        resp = _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "log_water", "cadence": "per_day", "target_count": 5}]},
            format="json",
        )
        assert resp.status_code == 201, resp.content[:400]
        assert PersonalPlan.objects.get(user=owner).source == "manual"

    def test_post_with_template_version_marks_source(self, seeded, owner, goal) -> None:
        proposal = _api().get(PROPOSAL_URL).json()["data"]
        resp = _api().post(
            PLAN_URL,
            {"actions": proposal["actions"], "template_version": proposal["template_version"]},
            format="json",
        )
        assert resp.status_code == 201, resp.content[:400]
        plan = PersonalPlan.objects.get(user=owner)
        assert plan.source == "template:self_care:v1"
        assert [(a.action_type, a.cadence, a.target_count) for a in plan.actions.all()] == [
            ("book_service", "per_2_weeks", 1),
            ("log_food", "per_week", 3),
            ("log_water", "per_day", 5),
        ]

    def test_post_with_an_unknown_template_version_is_400_and_creates_nothing(
        self, seeded, owner, goal,
    ) -> None:
        resp = _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "log_water", "cadence": "per_day", "target_count": 5}],
             "template_version": 7},
            format="json",
        )
        assert resp.status_code == 400, resp.content[:400]
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert PersonalPlan.objects.count() == 0

    def test_post_with_a_version_of_another_goal_is_400(self, seeded, owner, goal) -> None:
        """v2 существует у relax, но не у self_care вызывающего."""
        from wellness.models import PlanTemplate

        PlanTemplate.objects.filter(goal_key="relax").update(is_active=False)
        PlanTemplate.objects.create(goal_key="relax", version=2, actions=[], why_text="x")

        resp = _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "log_water", "cadence": "per_day", "target_count": 5}],
             "template_version": 2},
            format="json",
        )
        assert resp.status_code == 400, resp.content[:400]
        assert PersonalPlan.objects.count() == 0

    @pytest.mark.parametrize("bad", ["1", 0, -1, True, 1.5], ids=["str", "zero", "neg", "bool", "float"])
    def test_post_with_a_malformed_template_version_is_400(self, seeded, owner, goal, bad) -> None:
        resp = _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "log_water", "cadence": "per_day", "target_count": 5}],
             "template_version": bad},
            format="json",
        )
        assert resp.status_code == 400, resp.content[:400]
        assert PersonalPlan.objects.count() == 0

    def test_source_is_not_in_the_read_payload(self, seeded, owner, goal) -> None:
        """В-5 (DRF-2101): plan_lite несёт только форму обязательства и факты."""
        from wellness.plan_lite import plan_lite_payload

        _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "log_water", "cadence": "per_day", "target_count": 5}],
             "template_version": 1},
            format="json",
        )
        assert set(plan_lite_payload(owner)) == {"plan_id", "goal_key", "actions"}


# ─── 5. реестр стирания ──────────────────────────────────────────────────────


class TestErasureRegistry:
    def test_plan_template_points_at_no_user_and_registry_stays_decided(self) -> None:
        from users.deletion_executor import (
            pointers_to_user,
            undecided_pointers,
            undecided_subject_refs,
        )
        from wellness.models import PlanTemplate

        assert PlanTemplate._meta.label == "wellness.PlanTemplate"
        assert not [p for p in pointers_to_user() if p.startswith("wellness.PlanTemplate.")]
        assert "wellness.PersonalPlan.user" in pointers_to_user()  # перепись не пуста
        assert undecided_pointers() == {}
        assert undecided_subject_refs() == {}

    def test_personal_plan_source_is_a_plain_string_not_a_relation(self) -> None:
        field = PersonalPlan._meta.get_field("source")
        assert field.concrete and not field.is_relation
        assert field.default == "manual"

    def test_plan_lite_import_census_covers_the_templates_module(self) -> None:
        from wellness.tests.test_plan_lite_2101 import (
            FORBIDDEN_IMPORT_NAMES,
            _imported_names,
            _plan_lite_modules,
        )

        names = [m.name for m in _plan_lite_modules()]
        assert "plan_lite_templates.py" in names, names
        for m in _plan_lite_modules():
            hit = FORBIDDEN_IMPORT_NAMES & _imported_names(m.read_text(encoding="utf-8"))
            assert not hit, (m.name, sorted(hit))
