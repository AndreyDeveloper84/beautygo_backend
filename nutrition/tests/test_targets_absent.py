"""Ориентира нет — и наружу это доезжает ОТСУТСТВИЕМ КЛЮЧА (§82, §85).

Предмет
-------

Владелец 09.09.2026 снял обе фикции дневника разом:

* «Текущая плоская норма калорий для всех **удаляется**» —
  ``NUTRITION_DEFAULT_CALORIES_GOAL`` = 2000 ккал, одна на всех: ни
  роста, ни веса, ни возраста в этой ветке не было вовсе;
* «Формула воды ``30 мл × вес`` и прибавки за беременность или кормление
  **не используются без отдельно утверждённой методики**» —
  ``WATER_ML_PER_KG`` = 30 плюс 300 при беременности и 700 при кормлении.

Прибавки сняты ДВАЖДЫ: и как неутверждённая методика, и по разделу 7
решения — беременным и кормящим Ayla не рассчитывает вовсе.

Почему ноль не годится
----------------------

До этой правки «цели нет» выражалось нулём: ``calories_goal = 0``,
``water_goal_ml = 0``. Ноль внутри модуля читался правильно, но НАРУЖУ
он уезжал как значение — ключ в JSON присутствовал. Потребитель, который
про уговор не знает (а таких у ручки трое: Mini App, бот, legacy), видит
поле со значением и вправе его показать. «0 из 0 ккал · 0 %» — это не
«ориентира нет», это ориентир ноль.

Единственная непротиворечивая форма, и она же прямая цитата §65 и §82:
отсутствие доезжает **отсутствием ключа**. Экран не рисует ни цели, ни
шкалы, ни процента — а съеденное показывает.

Замеры
------

09.09.2026, ``pytest nutrition/tests/test_targets_absent.py``:
до правки — ``9 failed, 1 passed``; после — ``10 passed``.
Чем снято: ``python -m pytest ... --junitxml``.

Рассылка «отстаёшь по воде» решала, кому слать, по той же снятой
формуле — её замер живёт рядом с остальными замерами рассылки, в
``notifications/tests/test_retention_tasks.py``.

Сторож на обратный ход — ``test_targets_stay_absent.py``: он краснеет
при ПОДСТАНОВКЕ значения вместо отсутствия, то есть ловит не сегодняшний
дефект, а завтрашнее «разумное умолчание».
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz

import pytest
from rest_framework.test import APIClient


pytestmark = pytest.mark.django_db


SUMMARY_URL = "/api/v1/nutrition/summary/"
WATER_URL = "/api/v1/nutrition/water/"
WATER_TODAY_URL = "/api/v1/nutrition/water/today/"


# ---------------------------------------------------------------------------
# Fixtures — человек с ПОЛНОСТЬЮ пройденной анкетой питания
# ---------------------------------------------------------------------------
#
# Предусловие выбрано самое невыгодное для правки: анкета пройдена, вес
# указан, профиль пересчитан штатным путём. Именно у ТАКОГО человека
# старый код выдавал 30 × 70 = 2100 мл, и именно его экран обязан
# остаться без ориентира — методики ещё нет ни для кого.


@pytest.fixture
def anketa_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="tgt-client", password="x", role="client",
        phone="+79993330000",
    )
    Profile.objects.filter(user=u).update(full_name="Tgt", city="Penza")
    return u


@pytest.fixture
def anketa_profile(anketa_user):
    """Профиль, пересчитанный ШТАТНЫМ путём — через ``profile_upsert``.

    Не ``NutritionProfile.objects.create(daily_water_ml=...)``: тест, где
    обе стороны данных построил один автор, проверяет согласованность
    фикстуры, а не системы. Значение обязано родиться там, где оно
    рождается на пилоте.
    """
    from nutrition.services.profile_upsert_service import upsert_profile

    upsert_profile(
        user=anketa_user,
        external_user_id=str(anketa_user.id),
        payload={
            "gender": "female",
            "age": 30,
            "height_cm": 170,
            "weight_kg": 70.0,
            "activity_coefficient": 1.375,
            "goal": "maintain",
            "pace": "moderate",
            "complete": True,
        },
        idempotency_key=None,
    )
    from nutrition.models import NutritionProfile
    return NutritionProfile.objects.get(user_id=anketa_user.id)


@pytest.fixture
def auth_client(anketa_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=anketa_user)
    return c


# ---------------------------------------------------------------------------
# 1. Источник: формулы больше нет
# ---------------------------------------------------------------------------


class TestTheWaterFormulaIsGone:
    def test_compute_norms_produces_no_water_target(self) -> None:
        """``compute_norms`` не возвращает ориентир по жидкости вовсе.

        Не ноль и не ``None`` в поле — поля НЕТ. Пустое поле пережило бы
        правку и через неделю снова получило бы число «по умолчанию»;
        отсутствующее поле придётся заводить заново и объяснять зачем.
        """
        from nutrition.services.nutrition_profile_service import (
            ProfileInputs, compute_norms,
        )

        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=170, weight_kg=70.0,
        ))
        assert not hasattr(norms, "daily_water_ml"), (
            "ориентир по жидкости вернулся в ComputedNorms: "
            f"{getattr(norms, 'daily_water_ml', None)!r}"
        )

    def test_the_per_kg_constant_is_gone(self) -> None:
        """``WATER_ML_PER_KG`` и ``_water_target`` сняты из модуля.

        Утверждение об отсутствии ИМЕНИ, а не о значении: пока имя живо,
        вернуть его в расчёт — одна строка.
        """
        from nutrition.services import nutrition_profile_service as mod

        assert not hasattr(mod, "WATER_ML_PER_KG")
        assert not hasattr(mod, "_water_target")

    def test_pregnancy_and_breastfeeding_add_nothing(self) -> None:
        """Прибавки +300 и +700 сняты.

        По разделу 7 решения этим людям Ayla не рассчитывает вовсе —
        прибавка к несуществующему ориентиру не имеет смысла дважды.
        """
        from nutrition.services.nutrition_profile_service import (
            ProfileInputs, compute_norms,
        )

        for flag in ("pregnant", "breastfeeding"):
            norms = compute_norms(ProfileInputs(
                gender="female", age=30, height_cm=170, weight_kg=70.0,
                health_flags={flag: True},
            ))
            assert not hasattr(norms, "daily_water_ml"), flag

    def test_profile_row_is_not_written_with_a_target(
        self, anketa_profile,
    ) -> None:
        """Штатный upsert не записывает ориентир в строку профиля.

        Столбец ``daily_water_ml`` объявлен ``default=0`` и остаётся в
        схеме — миграция данных существующих клиентов это отдельный срез.
        Правка здесь в том, что НОВЫХ фиктивных значений не появляется.
        """
        assert anketa_profile.daily_water_ml == 0, (
            "формула снова записала ориентир в профиль: "
            f"{anketa_profile.daily_water_ml}"
        )


# ---------------------------------------------------------------------------
# 2. Граница: ключа нет в ответе ручки
# ---------------------------------------------------------------------------


class TestTheKeyIsAbsentAtTheBoundary:
    def test_summary_omits_both_targets(self, auth_client, anketa_profile) -> None:
        """Сводка дня не содержит ни ``calories_goal``, ни ``water_goal_ml``.

        Ключа нет — не ``null`` и не ``0``. ``null`` потребитель тоже
        вправе показать («цель: —»), а прочерк это уже сообщение о том,
        что цель есть, просто мы её не знаем.
        """
        resp = auth_client.get(SUMMARY_URL, {"date": "2026-04-29"})
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert "calories_goal" not in body, body.get("calories_goal")
        assert "water_goal_ml" not in body, body.get("water_goal_ml")
        # Съеденное при этом на месте: снимается ориентир, не факт.
        assert body["calories_total"] == 0
        assert body["water_ml"] == 0

    def test_summary_still_reports_what_was_eaten(
        self, auth_client, anketa_user, anketa_profile,
    ) -> None:
        """Контроль присутствия: ручка не онемела целиком.

        Утверждения выше — про ОТСУТСТВИЕ, и они прошли бы победно в
        мире, где ответ пуст или ручка сломана. Здесь запись есть, и её
        калории обязаны доехать.
        """
        from nutrition.models import FoodLog

        FoodLog.objects.create(
            user_id=anketa_user.id, dish_name="Гречка",
            calories=310.0, protein_g=12.0, fat_g=3.0, carbs_g=60.0,
            logged_at=datetime(2026, 4, 29, 12, 0, tzinfo=dt_tz.utc),
        )
        body = auth_client.get(SUMMARY_URL, {"date": "2026-04-29"}).json()["data"]
        assert body["calories_total"] == 310.0
        assert len(body["entries"]) == 1
        assert "calories_goal" not in body

    def test_water_post_omits_goal_and_percent(
        self, auth_client, anketa_profile,
    ) -> None:
        """POST стакана не возвращает ни нормы, ни процента.

        Процент — производная ориентира. Ориентира нет, значит процента
        нет тоже: доля от несуществующей нормы это не ноль процентов.
        """
        resp = auth_client.post(WATER_URL, {"amount_ml": 250}, format="json")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert "water_goal_ml" not in body, body.get("water_goal_ml")
        assert "water_pct" not in body, body.get("water_pct")
        assert body["water_ml"] == 250

    def test_water_today_omits_goal(self, auth_client, anketa_profile) -> None:
        resp = auth_client.get(WATER_TODAY_URL)
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert "water_goal_ml" not in body, body.get("water_goal_ml")
