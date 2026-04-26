"""Unit tests for ErrorCode enum + error_response() validation."""
from __future__ import annotations

import pytest

from core.errors import ErrorCode
from users.response import error_response


class TestErrorCodeEnum:
    def test_known_code_resolves(self):
        assert ErrorCode.is_known("VALIDATION_ERROR")
        assert ErrorCode.is_known("AI_UNAVAILABLE")
        assert ErrorCode.is_known("FOOD_NOT_RECOGNIZED")

    def test_unknown_code_rejects(self):
        assert not ErrorCode.is_known("VALIDTION_ERROR")  # typo
        assert not ErrorCode.is_known("DEFINITELY_NOT_A_CODE")

    def test_enum_member_value_matches_string(self):
        assert ErrorCode.VALIDATION_ERROR.value == "VALIDATION_ERROR"


class TestErrorResponseAssertion:
    def test_passes_known_string_code_through_in_debug(self, settings):
        settings.DEBUG = True
        resp = error_response("VALIDATION_ERROR", "msg")
        assert resp.data["error"]["code"] == "VALIDATION_ERROR"

    def test_passes_enum_member_through(self, settings):
        settings.DEBUG = True
        resp = error_response(ErrorCode.VALIDATION_ERROR, "msg")
        assert resp.data["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_code_raises_in_debug(self, settings):
        settings.DEBUG = True
        with pytest.raises(AssertionError, match="Unknown error code"):
            error_response("DEFINITELY_NOT_A_CODE", "msg")

    def test_unknown_code_does_not_break_in_production(self, settings):
        """In prod (DEBUG=False) an unknown code falls through — the
        response still goes out so a single typo doesn't 500 the API.
        Logging is best-effort (project conftest may swallow records);
        the load-bearing assertion is that the response is intact."""
        settings.DEBUG = False
        resp = error_response("STILL_NOT_A_CODE", "msg")
        assert resp.data["error"]["code"] == "STILL_NOT_A_CODE"
        assert resp.status_code == 400
