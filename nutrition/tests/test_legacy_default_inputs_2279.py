"""Прежние умолчания темпа и активности — ``legacy_default``, не ответы (CD §76, №32).

Решение владельца: «старые значения пометить как legacy_default и
запросить подтверждение, а не считать ответами человека». #543 (вопрос 59)
убрал подстановку для НОВЫХ расчётов; строки, записанные раньше, хранят
подставленное рядом с названным, и различить их по одному столбцу нельзя.

Что доказуемо подставлено (замер):

* **активность ровно 1.4** — умолчание схемы (``default=1.4`` до #543) и
  ``get_or_create(defaults=…)``. В боте такого ответа нет и не было
  (1.2 / 1.375 / 1.55 / 1.725), а подводка к 1.375 в столбец не писалась —
  только в снимок;
* **``activity_skipped``** — «Не знаю»: бот слал 1.375 за человека;
* **темп «moderate»** — до #543 расчёт писал свой темп обратно в столбец
  (``profile.pace = norms.pace``, а ``norms.pace`` = ``pace or "moderate"``):
  каждый пересчёт без темпа оставлял «moderate». Шага темпа в боте до
  #1972 (22.09) не было. Названный «moderate» (приложение, бот после
  #1972) от подставленного по строке НЕ отличим — поэтому помечается
  весь, двумя счётчиками: при цели без темпа и при цели с темпом;
* **темп «gentle»** расчёт не подставлял никогда — не трогается.

Пометка — отдельное поле ``legacy_default_inputs`` (JSON-список имён
входов), аддитивная миграция. НЕ ``health_flags`` (главное окно, 22.09):
решение DT-1 (§67) — «при ЛЮБОМ флаге здоровья внешняя LLM не вызывается»,
#534 исполняет его правилом «любой истинный ключ», и пометка в флагах
выключила бы модель всем помеченным; к тому же это не данные о здоровье.
Поле выгружается по ст. 14 (реестр полей #544): человек видит, какие
значения в его профиле не его ответы.

Помеченный вход — «не назван»: расчёт отказывает с его именем, а не считает
молча. Темп — только при цели с темпом (у «поддерживать» он в расчёт не
входит). Названное значение (в том числе то же самое — это и есть
подтверждение) снимает пометку. Пометку ставит только команда: через upsert
её не записать.

Команда ``mark_legacy_default_inputs``: без ``--apply`` только печатает.

* m1 — сухой прогон печатает группы и ничего не пишет;
* m2 — ``--apply`` метит ровно доказуемое; «gentle» и названная активность
  не тронуты; повтор — «Метить нечего»;
* c1 — помеченная активность / темп: расчёта нет, отказ называет поле;
* c2 — подтверждение (то же значение) снимает пометку, расчёт есть;
* c3 — пометка через upsert не пишется и не снимается; ``health_flags``
  команда не трогает (DT-1);
* c4 — помеченный темп при «поддерживать» расчёт не останавливает;
* d1 — ответ профиля называет помеченные входы (``legacy_default_inputs``);
* d2 — выгрузка по ст. 14 несёт поле.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.personal_calculation_consent import PERSONAL_CALCULATION
from users.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/internal/profile/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}
SERVICE_TOKEN = "svc-token-2279"
Source = NutritionProfile.TargetsSource

BODY = {"gender": "female", "age": 36, "height_cm": 170, "weight_kg": 67.0}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


def _user(name: str) -> User:
    return User.objects.create(username=f"bot:{name}", role="client", is_proxy=True)


def _profile(name: str, **fields) -> NutritionProfile:
    """A row as the database holds it — written before #543, by hand."""
    base = dict(BODY, goal="lose", pace="", activity_coefficient=None, health_flags={})
    base.setdefault("legacy_default_inputs", [])
    base.update(fields)
    return NutritionProfile.objects.create(user=_user(name), **base)


def _post(name: str, body: dict) -> dict:
    resp = APIClient().post(
        URL,
        {**body, "consent": CONSENT},
        format="json",
        HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
        HTTP_X_EXTERNAL_USER_ID=f"bot:{name}",
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["data"]


def _missing(profile: NutritionProfile) -> list[str]:
    for entry in profile.last_overrides_applied or []:
        if isinstance(entry, dict) and entry.get("reason") == "insufficient_inputs":
            return list(entry.get("fields") or [])
    return []


def _marks(profile: NutritionProfile) -> set[str]:
    profile.refresh_from_db()
    return set(profile.legacy_default_inputs or [])


def _run(*args: str) -> str:
    out = StringIO()
    call_command("mark_legacy_default_inputs", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def legacy_rows():
    return {
        "a14": _profile("a14", activity_coefficient=1.4, pace="gentle"),
        "skipped": _profile(
            "skipped", activity_coefficient=1.375, pace="gentle",
            health_flags={"activity_skipped": True},
        ),
        "pace_maintain": _profile(
            "pace_maintain", goal="maintain", pace="moderate", activity_coefficient=1.55,
        ),
        "pace_lose": _profile("pace_lose", goal="lose", pace="moderate", activity_coefficient=1.55),
        "named": _profile("named", goal="lose", pace="gentle", activity_coefficient=1.375),
    }


class TestCommand:
    def test_dry_run_prints_the_groups_and_writes_nothing(self, legacy_rows):
        out = _run()

        for group in ("activity_1_4", "activity_skipped", "pace_moderate_no_pace_goal",
                      "pace_moderate_pace_goal"):
            assert group in out
        assert "--apply" in out
        for row in legacy_rows.values():
            assert _marks(row) == set()

    def test_apply_marks_exactly_what_is_provable(self, legacy_rows):
        _run("--apply")

        assert _marks(legacy_rows["a14"]) == {"activity_coefficient"}
        assert _marks(legacy_rows["skipped"]) == {"activity_coefficient"}
        assert _marks(legacy_rows["pace_maintain"]) == {"pace"}
        assert _marks(legacy_rows["pace_lose"]) == {"pace"}
        # Named values stay named: gentle was never invented, 1.375 without
        # the skip flag is a chosen «light».
        assert _marks(legacy_rows["named"]) == set()
        # The values themselves are not rewritten — a mark, not an erasure.
        legacy_rows["a14"].refresh_from_db()
        assert legacy_rows["a14"].activity_coefficient == 1.4
        assert "Метить нечего" in _run("--apply")


class TestCalculation:
    def test_a_marked_input_is_not_an_answer(self, legacy_rows):
        _run("--apply")
        # «обнови вес» — the partial upsert the bot sends.
        _post("pace_lose", {"weight_kg": 66.0})
        p = NutritionProfile.objects.get(user__username="bot:pace_lose")
        assert p.daily_kcal is None
        assert p.targets_source == Source.NONE
        assert "pace" in _missing(p)

        _post("a14", {"weight_kg": 66.0})
        a = NutritionProfile.objects.get(user__username="bot:a14")
        assert "activity_coefficient" in _missing(a)

    def test_confirming_the_same_value_clears_the_mark(self, legacy_rows):
        _run("--apply")
        _post("pace_lose", {"pace": "moderate", "goal": "lose"})
        p = NutritionProfile.objects.get(user__username="bot:pace_lose")

        assert _marks(p) == set()
        assert p.pace == "moderate"
        assert p.daily_kcal and p.daily_kcal > 0

        _post("a14", {"activity_coefficient": 1.55})
        assert _marks(NutritionProfile.objects.get(user__username="bot:a14")) == set()

    def test_upsert_neither_writes_nor_clears_the_mark(self, legacy_rows):
        _run("--apply")
        _post("pace_lose", {"legacy_default_inputs": [], "weight_kg": 66.0})
        assert _marks(legacy_rows["pace_lose"]) == {"pace"}

        _post("named", {"legacy_default_inputs": ["pace"]})
        assert _marks(legacy_rows["named"]) == set()

    def test_the_command_leaves_health_flags_alone(self, legacy_rows):
        # DT-1: any health flag switches the external model off (#534).
        _run("--apply")
        a14 = legacy_rows["a14"]
        a14.refresh_from_db()
        assert a14.health_flags == {}
        skipped = legacy_rows["skipped"]
        skipped.refresh_from_db()
        assert skipped.health_flags == {"activity_skipped": True}

    def test_a_marked_pace_does_not_stop_maintain(self, legacy_rows):
        _run("--apply")
        _post("pace_maintain", {"weight_kg": 66.0})
        p = NutritionProfile.objects.get(user__username="bot:pace_maintain")
        assert _marks(p) == {"pace"}
        assert p.daily_kcal and p.daily_kcal > 0


class TestProfileSaysWhatIsUnconfirmed:
    def test_the_profile_names_marked_inputs(self, legacy_rows):
        _run("--apply")
        resp = APIClient().get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:pace_lose",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        data = resp.json()["data"]
        assert data["legacy_default_inputs"] == ["pace"]
        assert data["pace"] == "moderate"

    def test_the_article_14_export_carries_the_field(self, legacy_rows):
        from users.remembered_export import FIELDS

        exported, _excluded = FIELDS["nutrition.NutritionProfile"]
        assert exported.get("legacy_default_inputs") == "legacy_default_inputs"
