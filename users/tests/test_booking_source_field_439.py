"""Regression tests for #439 — booking_source field on SpecialistProfile.

The field is the toggle between two booking flows (ADR-0009 §Booking SoR
rule). A test pins the choices + default so a future innocent edit to
the enum doesn't break the booking engine's branch in a way that's only
caught at runtime against a real specialist row.
"""
from __future__ import annotations

import pytest

from users.models import SpecialistProfile, User


@pytest.mark.django_db
class TestBookingSourceField:
    def test_choices_are_ayla_local_and_yclients(self):
        choices = dict(SpecialistProfile.BookingSource.choices)
        assert set(choices.keys()) == {"ayla_local", "yclients"}

    def test_default_is_ayla_local(self):
        # Default for new rows. Existing rows are backfilled to this
        # value by the migration 0011 — verified separately by Django
        # default mechanism on AddField.
        u = User.objects.create_user(
            username="bs439", password="x", role="specialist",
            phone="+79991234567",
        )
        profile = u.specialist_profile
        assert profile.booking_source == "ayla_local"

    def test_yclients_company_id_blank_when_local(self):
        u = User.objects.create_user(
            username="bs439_local", password="x", role="specialist",
            phone="+79991234568",
        )
        profile = u.specialist_profile
        assert profile.yclients_company_id == ""

    def test_setting_yclients_source_persists(self):
        u = User.objects.create_user(
            username="bs439_yclients", password="x", role="specialist",
            phone="+79991234569",
        )
        profile = u.specialist_profile
        profile.booking_source = SpecialistProfile.BookingSource.YCLIENTS
        profile.yclients_company_id = "12345"
        profile.save(update_fields=["booking_source", "yclients_company_id"])
        profile.refresh_from_db()
        assert profile.booking_source == "yclients"
        assert profile.yclients_company_id == "12345"
