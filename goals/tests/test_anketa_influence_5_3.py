"""§5.3 решений владельца 11.09.2026 — шаг анкеты либо влияет по rule_id, либо говорит, что нет.

Замер ``docs/MEASUREMENT_AREA_FEELING_5_3.md`` (Ayla/docs): ответы на
``area`` и ``feeling`` пишутся и не читаются — 0 читателей значения,
0 влияния на выдачу, — а сами вопросы с прогрессом «шаг 1 из 3» читаются
человеком как учтённые. §5.3: «если система не может показать rule_id,
где поле повлияло, нельзя заявлять, что поле учтено».

Два среза:

* **S0** — пометка «на подбор пока не влияет» приходит с сервера в
  ``prompt`` шага (C-1: экран рисует, не решает), мини-приложению правка
  не нужна;
* **S5** — сторож класса DRF-1656 («решение принято, код верен прежней
  редакции»): у каждого шага либо ``rule_ids`` с читателем в
  ``RULE_READERS``, либо пометка. Сторож проверяется в обе стороны на
  подставных шагах — иначе он был бы правилом без доказательства.
"""
from __future__ import annotations

import pytest

from goals import anketa
from goals.anketa import (
    NO_INFLUENCE_NOTE,
    RULE_READERS,
    AnketaStep,
    influence_declaration_errors,
    shown_prompt,
)
from goals.decision_context import build_decision_context
from users.models import User


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="bot:anketa-53", password="x", role="client",
        phone="+79995005300", is_proxy=True,
    )


# ---------------------------------------------------------------------------
# S0 — пометка доезжает до человека
# ---------------------------------------------------------------------------


class TestNoInfluenceNoteIsShown:
    @pytest.mark.parametrize("step", anketa.ANKETA_STEPS, ids=lambda s: s.key)
    def test_every_narrowing_step_tells_the_truth_today(self, step):
        """Сегодня ни area, ни feeling ни на что не влияют — и это сказано."""
        assert step.rule_ids == ()
        assert NO_INFLUENCE_NOTE in shown_prompt(step)
        # Вопрос не подменён пометкой, а дополнен ею.
        assert shown_prompt(step).startswith(step.prompt)

    @pytest.mark.django_db
    def test_note_reaches_the_document_prompt(self, customer, settings):
        """То, что рисует экран, — ``prompt`` элемента ``missing``; пометка в нём."""
        settings.GOAL_ANKETA_ENABLED = True
        item = build_decision_context(customer)["missing"][0]
        assert item["step"] == anketa.ANKETA_STEPS[0].key
        assert NO_INFLUENCE_NOTE in item["prompt"]

    @pytest.mark.django_db
    def test_wire_shape_is_unchanged(self, customer, settings):
        """Мини-приложению правка не нужна: набор ключей элемента прежний."""
        settings.GOAL_ANKETA_ENABLED = True
        item = build_decision_context(customer)["missing"][0]
        assert set(item) == {
            "kind", "prompt", "step", "options", "allow_free_text", "progress",
        }

    @pytest.mark.django_db
    def test_final_step_influences_and_says_nothing_of_the_kind(self):
        """Цель — единственный ответ, который доезжает до выдачи.

        Положительная стража: без неё пометка могла бы стоять на ВСЕХ
        шагах, и тест на area/feeling зеленел бы на анкете, которая врёт
        про цель в обратную сторону.
        """
        final = anketa.next_step({s.key for s in anketa.ANKETA_STEPS})
        assert final.key == anketa.FINAL_STEP_KEY
        assert final.rule_ids == ("goal.category_match",)
        assert NO_INFLUENCE_NOTE not in shown_prompt(final)
        assert shown_prompt(final) == anketa.FINAL_STEP_PROMPT


# ---------------------------------------------------------------------------
# S5 — сторож: rule_id ↔ читатель ↔ пометка
# ---------------------------------------------------------------------------


class TestInfluenceDeclarationGuard:
    @pytest.mark.django_db
    def test_real_steps_are_clean(self):
        """Сам сторож на живых шагах — включая финальный."""
        final = anketa.next_step({s.key for s in anketa.ANKETA_STEPS})
        assert influence_declaration_errors((*anketa.ANKETA_STEPS, final)) == []

    def test_every_declared_reader_is_importable_and_callable(self):
        """RULE_READERS не должен ссылаться в пустоту — даже для правил, которые
        ни один шаг пока не носит."""
        import importlib

        for rule_id, path in RULE_READERS.items():
            module_name, _, attr = path.rpartition(".")
            reader = getattr(importlib.import_module(module_name), attr)
            assert callable(reader), rule_id

    # Ниже — сторож на подставных шагах: доказательство, что он ловит
    # каждую из четырёх поломок, а не молчит.

    def test_rule_without_reader_is_caught(self):
        step = AnketaStep(key="probe", prompt="?", rule_ids=("no.such.rule",))
        errors = influence_declaration_errors((step,))
        assert errors == ["probe: rule_id 'no.such.rule' без читателя в RULE_READERS"]

    def test_reader_that_does_not_import_is_caught(self, monkeypatch):
        monkeypatch.setitem(RULE_READERS, "probe.rule", "goals.no_such_module.reader")
        step = AnketaStep(key="probe", prompt="?", rule_ids=("probe.rule",))
        errors = influence_declaration_errors((step,))
        assert len(errors) == 1 and "не импортируется" in errors[0]

    def test_reader_that_is_not_callable_is_caught(self, monkeypatch):
        monkeypatch.setitem(RULE_READERS, "probe.rule", "goals.anketa.NO_INFLUENCE_NOTE")
        step = AnketaStep(key="probe", prompt="?", rule_ids=("probe.rule",))
        errors = influence_declaration_errors((step,))
        assert errors == ["probe: читатель 'goals.anketa.NO_INFLUENCE_NOTE' не вызываем"]

    def test_step_with_rules_must_not_carry_the_note(self, monkeypatch):
        """Обратная поломка: правило есть, а текст всё ещё говорит «не влияет»."""
        monkeypatch.setattr(anketa, "shown_prompt", lambda s: f"{s.prompt} {NO_INFLUENCE_NOTE}")
        step = AnketaStep(key="probe", prompt="?", rule_ids=("goal.category_match",))
        errors = influence_declaration_errors((step,))
        assert errors == ["probe: есть правила, но текст говорит «не влияет»"]

    def test_step_without_rules_and_without_note_is_caught(self, monkeypatch):
        """И прямая: правил нет, пометки нет — молчаливое обещание."""
        monkeypatch.setattr(anketa, "shown_prompt", lambda s: s.prompt)
        step = AnketaStep(key="probe", prompt="?")
        errors = influence_declaration_errors((step,))
        assert errors == ["probe: нет правил и нет пометки"]
