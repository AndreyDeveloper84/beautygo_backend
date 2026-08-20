"""DRF-1120 — pin ``Appointment.Status`` (ORM) as a subset of ``BookingStatus``
(domain).

The two enums exist for a real reason (module docstring in
``appointments/records_status.py``: the ORM enum is presentation-adjacent,
the domain enum is the operational state machine `_BOOKING_TRANSITIONS`
enforces) — that split is not what this test challenges.

What it does challenge: every value the ORM enum can hold must be a value
the domain state machine *knows about* (has transition rules for, even if
those rules are "terminal, no transitions"). ``IN_PROGRESS`` was declared
on ``Appointment.Status`` and referenced in 5 places (admin colouring,
records_api "upcoming" set, records_status projection) with no entry in
``_BOOKING_TRANSITIONS`` at all — nothing could transition into it except
a manual admin edit, and nothing could transition out of it once there,
because ``Appointment.booking_status`` does ``BookingStatus(self.status)``,
which raises ``ValueError`` on an unknown value, and every guard
(``can_cancel`` / ``can_complete`` / ``can_mark_no_show``) swallows that
``ValueError`` as "no, you can't" — including "can't get out of this
status". One admin click made the row permanently un-cancellable,
un-completable, and (per ``is_active`` swallowing the same ValueError)
released its slot for double-booking. See arch review 2026-08-15 §6.

This test is the guard: if ``Appointment.Status`` ever grows a value
``BookingStatus`` doesn't know about, it fails — before that value reaches
an admin dropdown.

Note on ``BookingStatus`` vs its transition table: every ``BookingStatus``
member has an entry in ``_BOOKING_TRANSITIONS`` — even the terminal ones
map to an explicit empty ``frozenset()`` rather than being absent — so
"is a ``BookingStatus`` member" and "has a transition-table entry" are the
same check here. The test uses the public enum, not the module-private
``_BOOKING_TRANSITIONS`` dict.
"""
from __future__ import annotations

from appointments.domain.value_objects import BookingStatus
from appointments.models import Appointment


class TestAppointmentStatusIsSubsetOfBookingStatus:
    def test_every_orm_status_is_a_known_domain_status(self):
        orm_values = {choice.value for choice in Appointment.Status}
        domain_values = {status.value for status in BookingStatus}

        unreachable = orm_values - domain_values
        assert not unreachable, (
            f"Appointment.Status has value(s) BookingStatus doesn't know "
            f"about: {unreachable}. Such a value is reachable only by a "
            "manual admin edit and then has no transition out — see "
            "DRF-1120. Either add it to BookingStatus + a transition-table "
            "entry (a deliberate product decision) or remove it from "
            "Appointment.Status."
        )

    def test_orm_values_construct_a_valid_booking_status(self):
        # Belt-and-suspenders on the same invariant, at the call site that
        # actually breaks in prod: Appointment.booking_status does
        # BookingStatus(self.status) and callers rely on that not raising.
        for choice in Appointment.Status:
            BookingStatus(choice.value)  # raises ValueError if not a member
