"""Сторож границы «что на C03 не спрашиваем» (DRF-1751, макет C03 P23).

Анкета границу держит по построению — но добавление шага ``district``
прошло бы молча. Теперь не пройдёт: сторож читает ключи шагов, ключи
опций и слова подписей (по началу слова).

Проба объявлена здесь же и исполняется машиной: подставные шаги
``district`` / «Сколько готовы платить?» / опция «Со скидкой» краснят
ровно этот сторож; настоящие слова C03 («утром», «реакция кожи»,
«оценка») его не трогают — иначе он молчал бы и на настоящем нарушении,
и его бы отключили.
"""
from __future__ import annotations

import pytest

from goals import anketa


class TestTodaysAnketaIsInsideTheBoundary:
    def test_no_step_option_or_label_names_a_forbidden_topic(self):
        assert anketa.c03_boundary_violations(anketa.ANKETA_STEPS) == []

    def test_goal_step_prompt_is_clean(self):
        step = anketa.AnketaStep(key="goal", prompt=anketa.GOAL_STEP_PROMPT)
        assert anketa.c03_boundary_violations((step,)) == []


class TestTheGuardSeesAViolation:
    @pytest.mark.parametrize(
        "step, fragment",
        [
            (
                anketa.AnketaStep(key="district", prompt="В каком районе?"),
                "ключ шага из запрещённого списка",
            ),
            (
                anketa.AnketaStep(
                    key="care", prompt="Что важно?", options=(("price", "Подешевле"),)
                ),
                "опция 'price'",
            ),
            (
                anketa.AnketaStep(key="care", prompt="Сколько готовы платить, ₽?"),
                "запрещённой темы",
            ),
            (
                anketa.AnketaStep(
                    key="care", prompt="Что важно?", options=(("x", "Со скидкой"),)
                ),
                "'скидк'",
            ),
            (
                anketa.AnketaStep(
                    key="care", prompt="Что важно?", prompt_by_goal=(("relax", "К какому мастеру?"),)
                ),
                "'мастер'",
            ),
            (
                anketa.AnketaStep(key="care", prompt="Какой салон удобнее?"),
                "'салон'",
            ),
        ],
    )
    def test_a_forbidden_step_is_named(self, step, fragment):
        errors = anketa.c03_boundary_violations((step,))
        assert errors and fragment in errors[0], errors

    def test_every_violation_is_listed_not_only_the_first(self):
        step = anketa.AnketaStep(
            key="price", prompt="Какой бюджет?", options=(("salon", "В салоне"),)
        )
        errors = anketa.c03_boundary_violations((step,))
        assert len(errors) >= 3, errors


class TestTheGuardStaysQuietOnRealC03Words:
    @pytest.mark.parametrize(
        "text",
        [
            "Отёчность чаще заметна утром",
            "Реакция кожи на солнце",
            "Оценка общего состояния",
            "Окно между делами",
            "Центр внимания — лицо",
        ],
    )
    def test_real_words_do_not_trip_it(self, text):
        assert anketa._forbidden_stem_in(text) is None

    def test_price_wording_trips_it(self):
        # Слепое пятно проверки по началу слова закрыто явной основой.
        assert anketa._forbidden_stem_in("Хочу подешевле") == "подешев"
        assert anketa._forbidden_stem_in("Дешевле не бывает") == "дешев"
