"""DRF-1339 → §82/§85: подстановки больше нет, а не «она видна».

Файл заводился под маркер: когда человек не называл вес,
``compute_norms`` молча подставлял ``DEFAULT_WEIGHT_KG`` — 70.0,
медиану пензенской аудитории, — и КАЖДОЕ число ниже по цепочке (BMR,
калории, макросы, вердикт лестницы BMR-floor) считалось от значения,
которого человек не говорил. DRF-1339 сделал этот факт ВИДИМЫМ:
машиночитаемое поле ``assumed_inputs`` и запись ``assumed_input`` в
аудите. Ни одно число при этом не менялось.

Маркер был честной полумерой и своё отработал: он назвал предмет и
позволил его измерить. Но признание — не отказ. Число всё равно
считалось, уезжало в профиль и показывалось человеку как ЕГО ориентир, а
``assumed_inputs`` жил в ответе API, куда экран не смотрит.

Владелец 09.09.2026 снял подстановку целиком (§82, §85; раздел 3.2
решения перечисляет возраст, рост, вес и пол как ОБЯЗАТЕЛЬНЫЕ входы).
Файл сохранён и перевёрнут — он теперь доказывает две вещи:

* неназванный вес отменяет расчёт, и у отказа есть имя
  (``insufficient_inputs`` с перечнем полей), а ``assumed_inputs``
  всегда пуст, потому что подставлять стало нечего;
* расчёт по НАЗВАННЫМ входам не сдвинулся ни на калорию — снимок
  ``all_known_*`` / ``real_weight_*`` заморожен ещё до DRF-1339 и
  пережил обе правки.

Второе — контроль присутствия: без него первая половина прошла бы
победно в мире, где ``compute_norms`` всегда возвращает нули.
"""
from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.nutrition_profile_service import (
    ProfileInputs,
    compute_norms,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-1339"
URL = "/api/v1/nutrition/internal/profile/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:1339", role="client", is_proxy=True,
    )


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:1339",
    }


# ===========================================================================
# HTTP layer — the marker
# ===========================================================================


class TestAssumedInputsMarker:
    """Маркер пуст всегда: подставлять больше нечего."""

    def test_no_weight_cancels_the_norms_and_keeps_weight_null(
        self, proxy_user, headers,
    ):
        c = APIClient()
        resp = c.post(URL, {
            "gender": "female", "age": 40, "height_cm": 165,
            "goal": "maintain", "pace": "moderate",
        }, format="json", **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        # Маркер пуст: подстановки не было, потому что её больше нет.
        assert body["assumed_inputs"] == []
        # Вес как был NULL, так и остался — и в ответе, и в базе.
        assert body["weight_kg"] is None
        profile = NutritionProfile.objects.get(user=proxy_user)
        assert profile.weight_kg is None
        # Норм НЕТ — вместо чисел от чужого тела ноль, и у отказа имя.
        assert profile.daily_kcal == 0
        assert profile.bmr == 0
        assert {
            "reason": "insufficient_inputs", "fields": ["weight_kg"],
        } in profile.last_overrides_applied
        # Старого признания в аудите тоже нет: оно означало «посчитали
        # от подставленного», а считать перестали.
        assert not [
            e for e in profile.last_overrides_applied
            if e.get("reason") == "assumed_input"
        ]

        # GET отвечает тем же.
        get_resp = c.get(URL, **headers)
        get_body = get_resp.json()["data"]
        assert get_body["assumed_inputs"] == []
        assert get_body["weight_kg"] is None

    def test_with_weight_marker_empty(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {
            "gender": "female", "age": 40, "height_cm": 165,
            "weight_kg": 70.0, "goal": "maintain", "pace": "moderate",
        }, format="json", **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["assumed_inputs"] == []
        assert body["weight_kg"] == 70.0
        profile = NutritionProfile.objects.get(user=proxy_user)
        assert profile.weight_kg == 70.0
        assert not [
            e for e in profile.last_overrides_applied
            if e.get("reason") == "assumed_input"
        ]

    def test_bmr_floor_verdict_needs_a_real_weight(self, db):
        """Вердикт лестницы BMR-floor выносится только по НАЗВАННОМУ весу.

        Тест назывался ``..._on_assumed_vs_real_weight_distinguishable`` и
        проверял, что два одинаковых вердикта различимы одним полем.
        Теперь их не два: на неназванном весе вердикта нет вовсе —
        выносить приговор «цель снижения тебе не подходит» по чужому телу
        было хуже, чем не выносить.
        """
        c = APIClient()
        payload = {
            "gender": "female", "age": 70, "height_cm": 150,
            "activity_coefficient": 1.0, "goal": "lose", "pace": "moderate",
        }

        User.objects.create(username="bot:1339-a", role="client", is_proxy=True)
        resp_assumed = c.post(URL, payload, format="json", **{
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:1339-a",
        })
        body_assumed = resp_assumed.json()["data"]
        # NEGATIVE: веса нет — вердикта нет, и цель человека не тронута.
        assert body_assumed["goal_overridden_by"] is None
        assert body_assumed["goal"] == "lose"
        assert body_assumed["norms"]["daily_kcal"] == 0

        User.objects.create(username="bot:1339-b", role="client", is_proxy=True)
        resp_real = c.post(URL, {**payload, "weight_kg": 45.0}, format="json", **{
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:1339-b",
        })
        body_real = resp_real.json()["data"]
        # POSITIVE: вес назван — лестница работает как работала. Без
        # этой половины отрицание выше прошло бы и в мире, где вердикт
        # не выносится никому.
        assert body_real["goal_overridden_by"] == "bmr_floor"
        assert body_real["goal"] == "maintain"
        assert body_real["norms"]["daily_kcal"] > 0

        # Маркер подстановки пуст в обоих случаях: подставлять нечего.
        assert body_assumed["assumed_inputs"] == []
        assert body_real["assumed_inputs"] == []


# ===========================================================================
# Снимок. Числа заморожены с кода ДО DRF-1339 (снято с origin/dev) и
# пережили две правки подряд. Половина с известными входами — по-прежнему
# доказательство неизменности: расхождение значит, что правка тронула
# расчёт, чего она делать не должна. Половина с неназванным весом
# (`_REFUSED_CASES`) читается наоборот: её числа теперь описание дефекта,
# а не эталон.
# ===========================================================================


_SNAPSHOT = {
    "all_known_maintain": {
        "inputs": ProfileInputs(
            gender="female", age=40, height_cm=165, weight_kg=70.0,
            activity_coefficient=1.4, goal="maintain", pace="moderate",
        ),
        "expected": {
            "bmr": 1370, "daily_kcal": 1918, "daily_protein_g": 98,
            "daily_fat_g": 64, "daily_carbs_g": 238,
            "goal": "maintain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
    "unknown_weight_maintain": {
        "inputs": ProfileInputs(
            gender="female", age=40, height_cm=165, weight_kg=None,
            activity_coefficient=1.4, goal="maintain", pace="moderate",
        ),
        "expected": {
            "bmr": 1370, "daily_kcal": 1918, "daily_protein_g": 98,
            "daily_fat_g": 64, "daily_carbs_g": 238,
            "goal": "maintain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
    "unknown_weight_lose_bmr_floor": {
        "inputs": ProfileInputs(
            gender="female", age=70, height_cm=150, weight_kg=None,
            activity_coefficient=1.0, goal="lose", pace="moderate",
        ),
        "expected": {
            "bmr": 1126, "daily_kcal": 1126, "daily_protein_g": 98,
            "daily_fat_g": 38, "daily_carbs_g": 99,
            "goal": "maintain", "pace": "gentle",
            "goal_overridden_by": "bmr_floor",
            "daily_vitamin_d_iu": 800, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 8,
            "daily_calcium_mg": 1200, "daily_magnesium_mg": 320,
            "daily_omega3_g": 1.1, "daily_fiber_g": 21,
            "overrides_applied": [
                {"reason": "bmr_floor",
                 "from": {"pace": "moderate"}, "to": {"pace": "gentle"}},
                {"reason": "bmr_floor",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "real_weight_lose_bmr_floor": {
        "inputs": ProfileInputs(
            gender="female", age=70, height_cm=150, weight_kg=45.0,
            activity_coefficient=1.0, goal="lose", pace="moderate",
        ),
        "expected": {
            "bmr": 876, "daily_kcal": 876, "daily_protein_g": 63,
            "daily_fat_g": 29, "daily_carbs_g": 90,
            "goal": "maintain", "pace": "gentle",
            "goal_overridden_by": "bmr_floor",
            "daily_vitamin_d_iu": 800, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 8,
            "daily_calcium_mg": 1200, "daily_magnesium_mg": 320,
            "daily_omega3_g": 1.1, "daily_fiber_g": 21,
            "overrides_applied": [
                {"reason": "bmr_floor",
                 "from": {"pace": "moderate"}, "to": {"pace": "gentle"}},
                {"reason": "bmr_floor",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "unknown_weight_lose_ok": {
        "inputs": ProfileInputs(
            gender="male", age=30, height_cm=180, weight_kg=None,
            activity_coefficient=1.6, goal="lose", pace="moderate",
        ),
        "expected": {
            "bmr": 1680, "daily_kcal": 2150, "daily_protein_g": 112,
            "daily_fat_g": 72, "daily_carbs_g": 264,
            "goal": "lose", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 90, "daily_iron_mg": 8,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 400,
            "daily_omega3_g": 1.6, "daily_fiber_g": 38,
            "overrides_applied": [],
        },
    },
    "unknown_weight_all_defaults": {
        "inputs": ProfileInputs(),
        "expected": {
            "bmr": 1370, "daily_kcal": 1918, "daily_protein_g": 98,
            "daily_fat_g": 64, "daily_carbs_g": 238,
            "goal": "maintain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
    "unknown_weight_pregnant": {
        "inputs": ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=None,
            activity_coefficient=1.4, goal="lose", pace="moderate",
            health_flags={"pregnant": True},
        ),
        "expected": {
            "bmr": 1420, "daily_kcal": 2188, "daily_protein_g": 123,
            "daily_fat_g": 73, "daily_carbs_g": 285,
            "goal": "maintain", "pace": "moderate",
            "goal_overridden_by": "pregnancy",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 85, "daily_iron_mg": 27,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.4, "daily_fiber_g": 28,
            "overrides_applied": [
                {"reason": "pregnancy",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "unknown_weight_ed": {
        "inputs": ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=None,
            activity_coefficient=1.4, goal="lose", pace="moderate",
            health_flags={"eating_disorder": True},
        ),
        "expected": {
            "bmr": 1420, "daily_kcal": 1988, "daily_protein_g": 98,
            "daily_fat_g": 66, "daily_carbs_g": 250,
            "goal": "maintain", "pace": "moderate",
            "goal_overridden_by": "eating_disorder",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [
                {"reason": "eating_disorder",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "real_weight_tone": {
        "inputs": ProfileInputs(
            gender="male", age=25, height_cm=175, weight_kg=80.0,
            activity_coefficient=1.5, goal="tone", pace="gentle",
        ),
        "expected": {
            "bmr": 1774, "daily_kcal": 2416, "daily_protein_g": 128,
            "daily_fat_g": 81, "daily_carbs_g": 295,
            "goal": "tone", "pace": "gentle", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 90, "daily_iron_mg": 8,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 400,
            "daily_omega3_g": 1.6, "daily_fiber_g": 38,
            "overrides_applied": [],
        },
    },
    "unknown_weight_gain": {
        "inputs": ProfileInputs(
            gender="female", age=35, height_cm=170, weight_kg=None,
            activity_coefficient=1.3, goal="gain", pace="moderate",
        ),
        "expected": {
            "bmr": 1426, "daily_kcal": 2040, "daily_protein_g": 98,
            "daily_fat_g": 68, "daily_carbs_g": 259,
            "goal": "gain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
}


#: Случаи из снимка, где ВЕС НЕ НАЗВАН. Раньше они считались от
#: ``DEFAULT_WEIGHT_KG`` и давали числа, неотличимые от настоящих; теперь
#: расчёта нет вовсе (§82, §85). Ключи перечислены явно, а не по префиксу
#: имени: имя — не доказательство содержимого, а список должен ломаться
#: при добавлении случая, а не молча его пропускать.
_REFUSED_CASES = (
    "unknown_weight_maintain",
    "unknown_weight_lose_bmr_floor",
    "unknown_weight_lose_ok",
    "unknown_weight_all_defaults",
    "unknown_weight_pregnant",
    "unknown_weight_ed",
    "unknown_weight_gain",
)


class TestComputedNormsSnapshot:
    """Снимок разделён надвое: известные входы и неназванный вес.

    Файл заводился как доказательство НЕИЗМЕННОСТИ: DRF-1339 добавлял
    маркер подстановки и обязан был не тронуть ни одного числа. Половина
    снимка при этом фиксировала числа, посчитанные ОТ ЧУЖОГО ТЕЛА
    (``DEFAULT_WEIGHT_KG = 70.0``, медиана пензенской аудитории) — и
    фиксировала их как эталон.

    Владелец снял подстановку 09.09.2026. Половина с известными входами
    осталась ровно тем, чем была: расчёт по названным человеком числам
    не сдвинулся ни на калорию, и это здесь доказывается. Половина с
    неназванным весом перевёрнута: расчёта больше нет, а у отказа есть
    имя.

    Ориентир по жидкости выброшен из всех снимков вместе с формулой
    ``30 мл × вес``, которая его считала.
    """

    @pytest.mark.parametrize(
        "case", sorted(set(_SNAPSHOT) - set(_REFUSED_CASES)),
    )
    def test_known_inputs_still_compute_the_same_numbers(self, case):
        """Правка НЕ сдвинула расчёт там, где входы названы."""
        norms = compute_norms(_SNAPSHOT[case]["inputs"])
        for field_name, expected in _SNAPSHOT[case]["expected"].items():
            actual = getattr(norms, field_name)
            assert actual == expected, (
                f"{case}.{field_name}: {actual!r} != snapshot {expected!r}"
            )

    @pytest.mark.parametrize("case", _REFUSED_CASES)
    def test_an_unnamed_weight_cancels_the_calculation(self, case):
        """Вес не назван — расчёта нет, и отказ назван по имени.

        Числа из старого снимка тут больше не эталон, а описание
        дефекта: ``unknown_weight_maintain`` давал те же 1370 ккал BMR,
        что и ``all_known_maintain``, потому что вес брался один и тот
        же — чужой.
        """
        norms = compute_norms(_SNAPSHOT[case]["inputs"])
        assert norms.bmr == 0
        assert norms.daily_kcal == 0
        assert norms.daily_protein_g == 0
        assert [o.get("reason") for o in norms.overrides_applied] == [
            "insufficient_inputs",
        ]
        assert norms.overrides_applied[0]["fields"] == ["weight_kg"]
