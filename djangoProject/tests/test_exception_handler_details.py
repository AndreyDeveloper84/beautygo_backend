"""Unit tests for ``_normalize_validation_details`` (DRF-1715)."""
from __future__ import annotations

from djangoProject.exception_handler import _normalize_validation_details as norm


def test_drf_316_list_form_becomes_index_keyed_object():
    assert norm([{"field": ["bad"]}, {"value": ["required"]}]) == {
        "0": {"field": ["bad"]},
        "1": {"value": ["required"]},
    }


def test_passing_items_are_dropped_not_kept_as_empty_objects():
    assert norm([{}, {"field": ["bad"]}, {}]) == {"1": {"field": ["bad"]}}


def test_drf_318_dict_form_is_returned_untouched():
    given = {"1": {"field": ["bad"]}}
    assert norm(given) is given


def test_field_keyed_dict_from_a_single_serializer_is_untouched():
    given = {"diet_type": ["bad"]}
    assert norm(given) is given


def test_a_list_of_messages_is_not_the_many_true_shape():
    given = ["Invalid input."]
    assert norm(given) is given


def test_an_all_passing_list_becomes_an_empty_object():
    assert norm([{}, {}]) == {}
