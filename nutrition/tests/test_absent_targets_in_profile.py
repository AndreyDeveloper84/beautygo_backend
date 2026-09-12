"""DRF-1623 N-c: отказ доезжает отсутствием и в профиле тоже.

Половина этого среза уже была сделана чужой правкой: сводка отдаёт
``calories_goal`` как ``None``, и :class:`OmitAbsentTargetsMixin` ключ
выбрасывает. Проверено, а не принято на веру по таблице срезов.

Оставался профиль. Его блок ``norms`` собирается руками, миксин его не
видит, и до этой правки он уезжал целиком: ``daily_kcal: 0``, ``bmr: 0``.
Ноль здесь не «ориентир ноль калорий» — такого не бывает, — а «расчёта не
было», и потребитель эти два утверждения различить не мог.

**Половина поставки живёт в другом репозитории.** Бот на своей границе
превращает отсутствие обратно в ноль (``int(norms.get("daily_kcal") or
0)`` при ``daily_kcal: int`` в типе), поэтому одна эта правка человеку не
видна. Слияние парное.
"""

from __future__ import annotations

import pytest

from nutrition.models import NutritionProfile

#: §103 N-b: пересчёт ориентиров — только с основанием. Эти тесты зовут
#: сервис напрямую, минуя сторож ручки (#324), поэтому утверждение о
#: согласии объявляется здесь как предусловие: предмет тестов — форма
#: отказа, а не сам сторож (он в ``test_targets_recompute_gate.py``).
CONSENT = {"type": "personal_calculation", "document_version": "v1"}


@pytest.mark.django_db
class TestAbsentTargetsLeaveNoZeroBehind:
    def _upsert(self, user, payload: dict) -> dict:
        from nutrition.services.profile_upsert_service import upsert_profile

        return upsert_profile(
            user=user,
            external_user_id="bot:max:absent-1",
            payload=payload,
            idempotency_key=None,
        )

    def test_a_refused_calculation_sends_no_norms_at_all(self, django_user_model) -> None:
        user = django_user_model.objects.create(username="absent-refused")

        body = self._upsert(user, {"consent": CONSENT, "gender": "female", "age": 30})

        # Положительная стража: отказ действительно произошёл и НАЗВАН —
        # без неё «норм нет» зеленело бы и на профиле, который вообще не
        # сохранился.
        reasons = [o["reason"] for o in body["overrides_applied"]]
        assert reasons == ["insufficient_inputs"]

        # Ключ на месте — «спросили, ориентиров нет» отличается от «блок
        # не приехал», как пустой список от отсутствующего.
        assert "norms" in body
        assert body["norms"] == {}

    def test_a_successful_calculation_still_sends_them(self, django_user_model) -> None:
        """Пара к предыдущему. Одиночная проверка «норм нет» зеленела бы и
        на сериализаторе, который не отдаёт их НИКОГДА."""
        user = django_user_model.objects.create(username="absent-ok")

        body = self._upsert(
            user,
            {
                "consent": CONSENT,
                "gender": "female",
                "age": 30,
                "height_cm": 168,
                "weight_kg": 62,
                "goal": "maintain",
            },
        )

        assert body["norms"]["daily_kcal"] > 0
        assert body["norms"]["bmr"] > 0

    def test_water_is_the_reference_when_computed_and_absent_when_refused(self, django_user_model) -> None:
        """Снятая формула не возвращается; вода — справочник по полу (раздел 4).

        Отказ — без воды вовсе. Состоявшийся расчёт — 2200 женщине по
        ``adult_beverages_reference_v1``, не ``30 × 62 = 1860``: число от
        веса не зависит, и версия методики едет рядом с ним.
        """
        user = django_user_model.objects.create(username="absent-water")

        refused = self._upsert(user, {"consent": CONSENT, "gender": "female"})
        assert refused["norms"] == {}
        assert "daily_water_ml" not in refused["norms"]

        computed = self._upsert(
            user,
            {
                "consent": CONSENT,
                "age": 30, "height_cm": 168, "weight_kg": 62, "goal": "maintain",
            },
        )
        assert computed["norms"]["daily_kcal"] > 0, "стража: расчёт состоялся"
        assert computed["norms"]["daily_water_ml"] == 2200
        assert computed["norms"]["daily_water_ml"] != 30 * 62
        assert computed["targets_provenance"]["method_versions"]["fluids"] == "adult_beverages_reference_v1"


@pytest.mark.django_db
class TestTheTwoWaysOfSayingItDoNotDrift:
    """Ответ говорит «ориентира нет» ДВАЖДЫ: пустым блоком ``norms`` и
    источником ``targets_source = none``.

    Две формы — страховка ровно до тех пор, пока они не разошлись. Разойдясь,
    они становятся вторым источником истины, и через полгода кто-то будет
    чинить один, не зная про другой. Этот тест и есть та скрепа.
    """

    def _upsert(self, user, payload: dict) -> dict:
        from nutrition.services.profile_upsert_service import upsert_profile

        return upsert_profile(
            user=user,
            external_user_id="bot:max:drift-1",
            payload=payload,
            idempotency_key=None,
        )

    @pytest.mark.parametrize(
        "payload,expect_targets",
        [
            ({"consent": CONSENT, "gender": "female", "age": 30}, False),
            (
                {
                    "consent": CONSENT,
                    "gender": "female", "age": 30, "height_cm": 168, "weight_kg": 62,
                },
                True,
            ),
        ],
        ids=["refused", "computed"],
    )
    def test_source_and_norms_always_agree(
        self, django_user_model, payload: dict, expect_targets: bool
    ) -> None:
        user = django_user_model.objects.create(username=f"drift-{expect_targets}")

        body = self._upsert(user, payload)

        has_norms = bool(body["norms"])
        says_none = body["targets_provenance"]["source"] == NutritionProfile.TargetsSource.NONE

        # Обе формы проверяются на ОДНОМ ответе: расхождение между ними и
        # есть то, что этот тест ловит.
        assert has_norms == expect_targets
        assert says_none is not expect_targets
        assert has_norms is not says_none, (
            "две формы разошлись: блок ориентиров и источник говорят разное"
        )
