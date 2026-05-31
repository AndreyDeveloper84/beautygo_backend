"""Tests for core.ayla_urls — handoff Block A → A4."""
from __future__ import annotations

import pytest
from django.test import override_settings

from core.ayla_urls import AylaUrlBuilder


class TestBaseNormalisation:
    def test_trailing_slash_is_dropped(self):
        b = AylaUrlBuilder(
            internal_base="https://internal.example.com/",
            public_base="https://api.example.com//",
        )
        assert b.internal_base == "https://internal.example.com"
        # Multiple trailing slashes collapse — rstrip is greedy.
        assert b.public_base == "https://api.example.com"

    def test_empty_base_is_tolerated_at_construct(self):
        # Construction must not raise — dev / staging may have only
        # one base set. The raise happens at call time on the missing
        # side, with a named env var in the message.
        b = AylaUrlBuilder(internal_base="", public_base="")
        with pytest.raises(RuntimeError, match="AYLA_INTERNAL_BASE_URL"):
            b.internal("/foo")
        with pytest.raises(RuntimeError, match="AYLA_PUBLIC_BASE_URL"):
            b.public("/foo")


class TestPathBuilding:
    @pytest.fixture
    def builder(self):
        return AylaUrlBuilder(
            internal_base="https://internal.example.com",
            public_base="https://api.example.com",
        )

    def test_leading_slash_is_optional(self, builder):
        assert builder.internal("/a/b") == builder.internal("a/b")

    def test_absolute_url_argument_is_rejected(self, builder):
        # The whole point of the builder is to own the scheme — a
        # caller that passes "https://other.example.com/foo" is
        # exactly the regression to prevent.
        with pytest.raises(ValueError, match="must be relative"):
            builder.internal("https://other.example.com/foo")

    def test_path_kwargs_are_url_quoted(self, builder):
        url = builder.internal("/payments/internal/{id}/retry/", id="abc/def")
        assert url == "https://internal.example.com/payments/internal/abc%2Fdef/retry/"

    def test_uuid_id_passes_through_safely(self, builder):
        uid = "11111111-1111-1111-1111-111111111111"
        url = builder.internal("/internal/me/bookings/{bid}/", bid=uid)
        assert url.endswith(f"/internal/me/bookings/{uid}/")

    def test_api_v1_shortcut_prepends_namespace(self, builder):
        url = builder.api_v1("/masters/internal/by-yclients-staff-ids/")
        assert (
            url
            == "https://internal.example.com/api/v1/masters/internal/by-yclients-staff-ids/"
        )

    def test_public_api_v1_uses_public_base(self, builder):
        url = builder.public_api_v1("/payments/return/")
        assert url == "https://api.example.com/api/v1/payments/return/"


class TestQueryStringBuilding:
    @pytest.fixture
    def builder(self):
        return AylaUrlBuilder(
            internal_base="https://internal.example.com",
            public_base="https://api.example.com",
        )

    def test_with_query_appends_to_clean_url(self, builder):
        url = builder.with_query("https://api.example.com/foo", a=1, b="two")
        assert "a=1" in url
        assert "b=two" in url
        assert url.startswith("https://api.example.com/foo?")

    def test_with_query_drops_empty_values(self, builder):
        # Empty values would serialise as "?key=" — a value the receiver
        # is likely to mis-parse as the literal empty string, masking
        # a missing-context bug as an empty filter.
        url = builder.with_query("https://api.example.com/foo", a=None, b="", c=3)
        assert url == "https://api.example.com/foo?c=3"

    def test_with_query_returns_url_unchanged_if_all_empty(self, builder):
        url = builder.with_query("https://api.example.com/foo", a=None, b="")
        assert url == "https://api.example.com/foo"

    def test_with_query_appends_with_existing_query(self, builder):
        url = builder.with_query("https://api.example.com/foo?x=1", y=2)
        assert "x=1" in url
        assert "y=2" in url
        assert url.count("?") == 1  # only one ? overall


class TestFromSettings:
    @override_settings(
        AYLA_INTERNAL_BASE_URL="https://internal.example.com",
        AYLA_PUBLIC_BASE_URL="https://api.example.com",
    )
    def test_from_settings_reads_both_bases(self):
        b = AylaUrlBuilder.from_settings()
        assert b.internal_base == "https://internal.example.com"
        assert b.public_base == "https://api.example.com"

    @override_settings(AYLA_INTERNAL_BASE_URL="", AYLA_PUBLIC_BASE_URL="")
    def test_from_settings_tolerates_empty(self):
        # Equivalent to the "missing env var" state in unit-test runs.
        # Construction succeeds; only the actual call raises.
        b = AylaUrlBuilder.from_settings()
        with pytest.raises(RuntimeError, match="AYLA_INTERNAL_BASE_URL"):
            b.internal("/foo")
