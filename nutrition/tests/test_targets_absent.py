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
            # §103 N-b: пересчёт только с основанием — предусловие.
            "consent": {"type": "personal_calculation", "document_version": "v1"},
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
    def test_compute_norms_water_is_the_reference_by_sex_not_the_formula(self) -> None:
        """Ориентир по жидкости — справочник по полу (раздел 4), не 30 × вес.

        Поле ``daily_water_ml`` в ``ComputedNorms`` вернулось ВМЕСТЕ с
        версией методики ``adult_beverages_reference_v1`` — это и есть
        «отдельно утверждённая методика», без которой §82 запрещал
        любое число. Сторож на форму: число не зависит от веса (тот же
        пол, разный вес — то же число) и не совпадает с 30 × вес.
        """
        from nutrition.services.nutrition_profile_service import (
            FLUIDS_METHOD_VERSION, ProfileInputs, compute_norms,
        )

        light = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=170, weight_kg=50.0,
        ))
        heavy = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=170, weight_kg=90.0,
        ))
        male = compute_norms(ProfileInputs(
            gender="male", age=30, height_cm=180, weight_kg=80.0,
        ))
        assert light.daily_water_ml == heavy.daily_water_ml == 2200
        assert male.daily_water_ml == 3000
        assert light.daily_water_ml != 30 * 50 and heavy.daily_water_ml != 30 * 90
        assert light.method_versions["fluids"] == FLUIDS_METHOD_VERSION == "adult_beverages_reference_v1"

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
            # N-g: health-фактор — отказ; воды нет вместе со всем, и
            # версии методики жидкости у отказа нет.
            assert norms.daily_water_ml is None, flag
            assert "fluids" not in norms.method_versions, flag

    def test_profile_row_carries_the_reference_as_a_proposal(
        self, anketa_profile,
    ) -> None:
        """Штатный upsert пишет справочник по полу — как ПРЕДЛОЖЕНИЕ.

        Не 30 × вес (анкета в фикстуре — 70 кг: формула дала бы 2100,
        справочник даёт 2200 женщине), и не действующий ориентир: строка
        в ``ayla_proposed`` до подтверждения. Остаток снятой формулы у
        старых строк стирает команда ``clear_targets_without_provenance``.
        """
        assert anketa_profile.daily_water_ml == 2200, anketa_profile.daily_water_ml
        assert anketa_profile.daily_water_ml != 30 * 70
        assert anketa_profile.targets_source == "ayla_proposed"
        assert anketa_profile.targets_method_versions["fluids"] == "adult_beverages_reference_v1"


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


# ---------------------------------------------------------------------------
# 3. Чужое тело: медиана пензенской аудитории за пропущенное поле
# ---------------------------------------------------------------------------


class TestNobodyGetsSomeoneElsesBody:
    """Пропущенный рост, вес, возраст или пол не заменяются медианой.

    В модуле стояло::

        DEFAULT_GENDER = "female"
        DEFAULT_AGE = 40
        DEFAULT_HEIGHT_CM = 165
        DEFAULT_WEIGHT_KG = 70.0   # «Penza pilot audience median»

    и подставлялось за ЛЮБОЕ незаполненное поле. Человек, не назвавший
    вес, получал ориентир, посчитанный **от чужого тела**, — а на экране
    это неотличимо от своего.

    Это тяжелее плоской константы, а не легче. Плоскую 2000 видно: она
    одинаковая у всех, и рано или поздно кто-то замечает. Медиана даёт
    ПРАВДОПОДОБНОЕ и РАЗНОЕ число — оно меняется от ответов человека и
    потому выглядит персональным. Оспорить его нельзя: не с чем сверить.

    DRF-1339 завёл маркер ``{"reason": "assumed_input", "field":
    "weight_kg"}``, но маркер — это признание, а не отказ: число всё
    равно считалось, уезжало в профиль и показывалось. У отсутствия
    должно быть имя, а не сноска под подставленным значением.
    """

    REQUIRED = ("gender", "age", "height_cm", "weight_kg")

    def test_the_median_body_constants_are_gone(self) -> None:
        from nutrition.services import nutrition_profile_service as mod

        alive = [
            n for n in
            ("DEFAULT_GENDER", "DEFAULT_AGE", "DEFAULT_HEIGHT_CM", "DEFAULT_WEIGHT_KG")
            if hasattr(mod, n)
        ]
        assert alive == [], f"медиана вернулась в модуль: {alive}"

    def test_each_missing_field_alone_refuses_the_whole_calculation(self) -> None:
        """Любого ОДНОГО пропуска достаточно, чтобы расчёта не было.

        Проверяются все четыре по одному, а не «пустой ввод»: пустой
        ввод прошёл бы и в мире, где подстановка осталась для трёх полей
        из четырёх.
        """
        from nutrition.services.nutrition_profile_service import (
            ProfileInputs, compute_norms,
        )

        complete = {
            "gender": "female", "age": 30,
            "height_cm": 170, "weight_kg": 70.0,
        }
        for field_name in self.REQUIRED:
            kwargs = dict(complete)
            kwargs[field_name] = None if field_name != "gender" else ""
            norms = compute_norms(ProfileInputs(**kwargs))
            # ``None``, не ноль (§103): отказ — отсутствие, а не число.
            assert norms.bmr is None, f"{field_name}: bmr={norms.bmr}"
            assert norms.daily_kcal is None, f"{field_name}: kcal={norms.daily_kcal}"
            assert norms.daily_protein_g is None, field_name
            reasons = [o.get("reason") for o in norms.overrides_applied]
            assert "insufficient_inputs" in reasons, (
                f"{field_name}: у пропуска нет имени — {norms.overrides_applied}"
            )
            named = [
                o for o in norms.overrides_applied
                if o.get("reason") == "insufficient_inputs"
            ][0]
            assert field_name in named.get("fields", []), named

    def test_a_complete_anketa_still_gets_a_number(self) -> None:
        """Контроль присутствия: отказ адресный, а не поголовный.

        Три утверждения выше — про ОТСУТСТВИЕ, и все три прошли бы
        победно в мире, где ``compute_norms`` сломан и всегда возвращает
        нули. Полная анкета обязана считаться.

        Число здесь НЕ сверяется с эталоном: методика калорий (§85 —
        Миффлин — Сан Жеор, поправка не более ±10%) это следующий срез,
        и нынешние ``GOAL_FACTORS`` ей не соответствуют. Проверяется
        ровно то, что расчёт состоялся.
        """
        from nutrition.services.nutrition_profile_service import (
            ProfileInputs, compute_norms,
        )

        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=170, weight_kg=70.0,
        ))
        assert norms.bmr > 0
        assert norms.daily_kcal > 0
        assert "insufficient_inputs" not in [
            o.get("reason") for o in norms.overrides_applied
        ]

    def test_the_profile_row_of_a_person_who_skipped_weight_stays_empty(
        self, anketa_user,
    ) -> None:
        """Штатный upsert без веса не записывает ориентиры в профиль."""
        from nutrition.models import NutritionProfile
        from nutrition.services.profile_upsert_service import upsert_profile

        upsert_profile(
            user=anketa_user,
            external_user_id=str(anketa_user.id),
            payload={
                # §103 N-b: основание есть, входов — нет: отказ по входам.
                "consent": {"type": "personal_calculation", "document_version": "v1"},
                "gender": "female", "age": 30, "height_cm": 170,
                # веса нет — человек его не назвал
                "goal": "maintain", "complete": True,
            },
            idempotency_key=None,
        )
        row = NutritionProfile.objects.get(user_id=anketa_user.id)
        assert row.daily_kcal is None, (
            f"человеку без веса записали {row.daily_kcal} ккал от чужого тела"
        )
        assert row.bmr is None
