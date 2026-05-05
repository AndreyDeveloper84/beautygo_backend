"""Tests for ai.personal_context_hint — UserPersonalContext → prompt block.

Pure-function module — no Django ORM, just shape-translation. Uses
in-memory `SimpleNamespace` stand-ins for the model so the test file
stays fast and doesn't require a DB roundtrip.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from ai.personal_context_hint import format_personal_context_hint


def _ctx(**kwargs):
    """Build a fake UserPersonalContext-like object with sensible defaults."""
    return SimpleNamespace(
        preferred_districts=kwargs.get("preferred_districts", []),
        preferred_time_slots=kwargs.get("preferred_time_slots", []),
        price_range_min=kwargs.get("price_range_min"),
        price_range_max=kwargs.get("price_range_max"),
        diet_type=kwargs.get("diet_type", ""),
        skin_sensitivities=kwargs.get("skin_sensitivities", []),
        prefers_flexible_cancellation=kwargs.get(
            "prefers_flexible_cancellation", False,
        ),
    )


# ---------------------------------------------------------------------------
# Empty / missing context
# ---------------------------------------------------------------------------


class TestEmptyCases:
    def test_none_returns_empty_string(self):
        assert format_personal_context_hint(None) == ""

    def test_all_defaults_returns_empty_string(self):
        # Every field empty / None / False — no fields actually set.
        assert format_personal_context_hint(_ctx()) == ""

    def test_empty_lists_treated_as_unset(self):
        ctx = _ctx(
            preferred_districts=[],
            preferred_time_slots=[],
            skin_sensitivities=[],
        )
        assert format_personal_context_hint(ctx) == ""


# ---------------------------------------------------------------------------
# Field rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_districts_listed(self):
        ctx = _ctx(preferred_districts=["Тверская", "Преображенская"])
        out = format_personal_context_hint(ctx)
        assert "ИЗВЕСТНЫЕ ПРЕДПОЧТЕНИЯ КЛИЕНТА" in out
        assert "Тверская" in out
        assert "Преображенская" in out

    def test_districts_capped_at_5_with_overflow_suffix(self):
        many = [f"D{i}" for i in range(8)]
        out = format_personal_context_hint(_ctx(preferred_districts=many))
        # First 5 shown, others rolled up.
        for d in many[:5]:
            assert d in out
        assert "+ещё 3" in out
        # Overflow ones are not in the rendered text.
        assert "D6" not in out
        assert "D7" not in out

    def test_time_slots_use_human_labels(self):
        ctx = _ctx(preferred_time_slots=["evening", "morning"])
        out = format_personal_context_hint(ctx)
        assert "вечер (17–21)" in out
        assert "утро (9–12)" in out
        # Raw enum keys should not leak.
        assert "evening" not in out
        assert "morning" not in out

    def test_unknown_time_slot_falls_back_to_raw_key(self):
        # Defensive — if the model adds a new enum value before this
        # mapping is updated, the raw key shows up rather than crashing.
        ctx = _ctx(preferred_time_slots=["midnight"])
        out = format_personal_context_hint(ctx)
        assert "midnight" in out

    def test_budget_range_both_bounds(self):
        ctx = _ctx(
            price_range_min=Decimal("2000.00"),
            price_range_max=Decimal("5000.00"),
        )
        out = format_personal_context_hint(ctx)
        assert "2000–5000 ₽" in out

    def test_budget_min_only(self):
        out = format_personal_context_hint(_ctx(price_range_min=Decimal("3500")))
        assert "от 3500 ₽" in out
        assert "–" not in out  # not a range

    def test_budget_max_only(self):
        out = format_personal_context_hint(_ctx(price_range_max=Decimal("7000")))
        assert "до 7000 ₽" in out

    def test_budget_keeps_decimal_for_non_round_amounts(self):
        ctx = _ctx(
            price_range_min=Decimal("1500.50"),
            price_range_max=Decimal("3000.00"),
        )
        out = format_personal_context_hint(ctx)
        assert "1500.5" in out
        assert "3000" in out

    def test_diet_uses_human_label(self):
        out = format_personal_context_hint(_ctx(diet_type="vegan"))
        assert "веганство" in out
        assert "vegan" not in out

    def test_unknown_diet_falls_back_to_raw_value(self):
        out = format_personal_context_hint(_ctx(diet_type="future_diet"))
        assert "future_diet" in out

    def test_skin_sensitivities_listed(self):
        out = format_personal_context_hint(
            _ctx(skin_sensitivities=["парабены", "силиконы"]),
        )
        assert "парабены" in out
        assert "силиконы" in out

    def test_skin_sensitivities_capped_at_8_with_suffix(self):
        many = [f"alg{i}" for i in range(12)]
        out = format_personal_context_hint(_ctx(skin_sensitivities=many))
        for a in many[:8]:
            assert a in out
        assert "+ещё 4" in out
        assert "alg10" not in out

    def test_flexible_cancellation_renders_when_true(self):
        out = format_personal_context_hint(
            _ctx(prefers_flexible_cancellation=True),
        )
        assert "гибкую политику отмены" in out

    def test_flexible_cancellation_suppressed_when_false(self):
        # False is the default; it should not produce a bullet.
        out = format_personal_context_hint(_ctx(diet_type="vegan"))
        assert "отмен" not in out


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestComposition:
    def test_multiple_fields_render_as_bullet_list(self):
        ctx = _ctx(
            preferred_districts=["Тверская"],
            preferred_time_slots=["evening"],
            price_range_max=Decimal("5000"),
            diet_type="vegetarian",
            skin_sensitivities=["парабены"],
            prefers_flexible_cancellation=True,
        )
        out = format_personal_context_hint(ctx)
        # Six fields, six bullet lines (plus the header line).
        assert out.count("\n- ") == 6

    def test_only_one_field_set_renders_minimally(self):
        out = format_personal_context_hint(_ctx(diet_type="halal"))
        assert "ИЗВЕСТНЫЕ ПРЕДПОЧТЕНИЯ КЛИЕНТА" in out
        assert out.count("\n- ") == 1
        assert "халяль" in out
