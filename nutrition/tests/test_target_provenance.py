"""DRF-1623 N-d: у ориентира появляется происхождение.

До этого среза значение ориентира не отвечало на вопрос, что за ним
стоит: grep по ``method_version``, ``input_snapshot`` и ``target_source``
в ``nutrition/**`` давал ноль. Замер пилота 10.09.2026 показал цену — у
двух профилей из шести полный набор ориентиров при пустых весе, росте и
возрасте, то есть числа от подставленной медианы лежали рядом с числами
от настоящих данных и ничем от них не отличались.

Проверяется не наличие полей (это тавтология), а три свойства, каждое из
которых можно сломать, оставив поля на месте:

* расчёт СОБИРАЕТ своё происхождение сам — снимок приезжает вместе с
  результатом, а не собирается вызывающей стороной по памяти;
* отказ НЕ выдаётся за расчёт — у несостоявшегося расчёта происхождения
  нет, и прежнее не остаётся висеть рядом с обнулённым значением;
* происхождение доезжает НАРУЖУ, потому что §92 п.5 адресован показу,
  а снимок входов наружу не уезжает, потому что показу он не нужен.
"""

from __future__ import annotations

import pytest

from nutrition.services.nutrition_profile_service import (
    CALORIES_METHOD_VERSION,
    SNAPSHOT_INPUTS,
    ProfileInputs,
    compute_norms,
)


def _full_inputs(**overrides) -> ProfileInputs:
    base = dict(
        gender="female",
        age=30,
        height_cm=168,
        weight_kg=62.0,
        activity_coefficient=1.375,
        goal="maintain",
        pace="moderate",
    )
    base.update(overrides)
    return ProfileInputs(**base)


class TestTheCalculationCarriesItsOwnProvenance:
    def test_a_successful_calculation_reports_version_and_inputs(self) -> None:
        norms = compute_norms(_full_inputs())

        # Положительная стража прежде проверок происхождения: расчёт
        # действительно состоялся. Без неё «версия на месте» зеленело бы
        # и у функции, вернувшей нули с заполненными метаданными.
        assert norms.daily_kcal > 0
        assert norms.computed is True

        assert norms.method_versions == {"calories": CALORIES_METHOD_VERSION}
        # Снимок собран по объявленному списку, а не по случайному набору
        # полей: разъехавшись, они разошлись бы молча.
        assert set(norms.input_snapshot) == set(SNAPSHOT_INPUTS)
        assert norms.input_snapshot["weight_kg"] == 62.0
        assert norms.input_snapshot["gender"] == "female"

    def test_the_snapshot_records_what_was_computed_from_not_what_was_asked(self) -> None:
        """Лестница переопределений могла изменить цель — в снимке то,
        ПО ЧЕМУ считали.

        Иначе снимок обещал бы воспроизводимость и не давал её: повторив
        расчёт по записанной цели, получили бы другое число.
        """
        norms = compute_norms(_full_inputs(goal="lose", health_flags={"eating_disorder": True}))

        # РПП переводит цель в maintain — это уже проверяют соседние
        # тесты; здесь важно, что снимок согласен с результатом.
        assert norms.goal == "maintain"
        assert norms.input_snapshot["goal"] == norms.goal

    def test_recomputing_the_same_inputs_gives_the_same_snapshot(self) -> None:
        """§85 — воспроизводимость. Тот же вход, тот же снимок и версия."""
        first = compute_norms(_full_inputs())
        second = compute_norms(_full_inputs())

        assert first.daily_kcal == second.daily_kcal
        assert first.input_snapshot == second.input_snapshot
        assert first.method_versions == second.method_versions


class TestARefusalIsNotDressedAsACalculation:
    @pytest.mark.parametrize("missing", ["weight_kg", "height_cm", "age", "gender"])
    def test_missing_input_leaves_no_provenance(self, missing: str) -> None:
        """Нет расчёта — нет и происхождения.

        Заполнить версию и снимок на отказе значило бы выдать
        несостоявшийся расчёт за состоявшийся: потребитель увидел бы
        объяснение у числа, которого нет.
        """
        norms = compute_norms(_full_inputs(**{missing: None}))

        # Положительная стража: отказ действительно произошёл и НАЗВАН.
        assert norms.daily_kcal == 0
        assert [o["reason"] for o in norms.overrides_applied] == ["insufficient_inputs"]

        assert norms.computed is False
        assert norms.method_versions == {}
        assert norms.input_snapshot == {}


@pytest.mark.django_db
class TestProvenanceIsStoredAndLeavesTheService:
    """Замер на стыке: от записи профиля до ответа ручки, без подмен."""

    def _upsert(self, user, payload: dict) -> dict:
        from nutrition.services.profile_upsert_service import upsert_profile

        return upsert_profile(
            user=user,
            external_user_id="bot:max:provenance-1",
            payload=payload,
            idempotency_key=None,
        )

    def test_calculated_targets_carry_their_source_outward(self, django_user_model) -> None:
        user = django_user_model.objects.create(username="provenance-ok")

        body = self._upsert(
            user,
            {
                "gender": "female",
                "age": 30,
                "height_cm": 168,
                "weight_kg": 62,
                "activity_coefficient": 1.375,
                "goal": "maintain",
            },
        )

        assert body["norms"]["daily_kcal"] > 0, "стража: ориентир действительно посчитан"
        provenance = body["targets_provenance"]
        assert provenance["source"] == "ayla_calculated"
        assert provenance["method_versions"] == {"calories": CALORIES_METHOD_VERSION}
        assert provenance["computed_at"] is not None

        # Снимок входов наружу НЕ уходит: он для воспроизводимости на
        # нашей стороне, а не для экрана. Отдать его значило бы разослать
        # параметры тела туда, где они не нужны.
        assert "input_snapshot" not in provenance

    def test_a_refused_calculation_reports_no_target_not_a_stale_source(
        self, django_user_model
    ) -> None:
        """Пара к предыдущему, и она ловит то, что одиночный тест пропустит.

        Сначала расчёт проходит и происхождение записывается. Потом вес
        убирают — расчёт отменяется, значение обнуляется. Прежнее
        происхождение обязано уйти вместе с ним: объяснение, оставшееся
        рядом с нулём, объясняет число, которого больше нет, и выглядит
        оно убедительнее, чем пустота.
        """
        user = django_user_model.objects.create(username="provenance-lost")

        first = self._upsert(
            user,
            {
                "gender": "female",
                "age": 30,
                "height_cm": 168,
                "weight_kg": 62,
                "goal": "maintain",
            },
        )
        assert first["targets_provenance"]["source"] == "ayla_calculated"

        second = self._upsert(user, {"weight_kg": None})

        assert second["norms"]["daily_kcal"] == 0, "стража: расчёт действительно отменён"
        provenance = second["targets_provenance"]
        assert provenance["source"] == "none"
        assert provenance["method_versions"] == {}
        assert provenance["computed_at"] is None
