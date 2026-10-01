"""DRF-2689 — ``complete_elapsed_backlog`` writes only where told to, on the record.

Closing a visit charges the platform fee. The command had a dry run, a window
and a cap, but wrote on whatever database the shell was connected to and left
nothing behind but stdout. Pairs that must differ:

* b1 — a run that writes without ``--database`` / naming another database /
  without ``--operator`` is a loud refusal and closes nothing; with both it
  closes the visit;
* b2 — ``--dry-run`` is what it was: needs neither key, writes nothing;
* b3 — every run leaves one receipt; a real one carries the counters;
* b4 — the receipt exists BEFORE the first closure, and no receipt means
  nothing is closed.

The database name is read from the live connection, not from the helper
under test.
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.utils import timezone

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from appointments.models import Appointment, OutboxEvent
from appointments.tests.test_auto_complete_1064 import (  # noqa: F401 — fixtures
    _booking,
    client_user,
    salon,
    service,
    specialist,
)
from payments import money_commands

pytestmark = pytest.mark.django_db(transaction=True)

NAME = event_catalogue.COMPLETE_ELAPSED_BACKLOG_RUN
SWEEP = "appointments.management.commands.complete_elapsed_backlog.complete_elapsed_bookings"
RECEIPT = "appointments.management.commands.complete_elapsed_backlog.write_receipt"


def _db() -> str:
    return connection.settings_dict["NAME"]


def _run(*args: str) -> str:
    out, err = StringIO(), StringIO()
    since = (timezone.now() - timedelta(days=30)).date().isoformat()
    call_command("complete_elapsed_backlog", "--since", since, *args, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


def _receipts() -> list[AnalyticsEvent]:
    return list(AnalyticsEvent.objects.filter(event_name=NAME).order_by("created_at"))


@pytest.fixture
def elapsed(client_user, specialist, service) -> Appointment:  # noqa: F811
    return _booking(client_user, specialist, service, ended_hours_ago=24 * 9)


def _untouched(appt: Appointment) -> bool:
    appt.refresh_from_db()
    return appt.status == Appointment.Status.CONFIRMED and not OutboxEvent.objects.exists()


class TestB1TheWriteIsNamed:
    def test_no_database_is_a_refusal(self, elapsed):
        with pytest.raises(CommandError, match="--database is required"):
            _run("--operator", "ops")

        assert _untouched(elapsed)
        assert _receipts() == []

    def test_another_database_is_a_refusal(self, elapsed):
        with pytest.raises(CommandError, match="is not the database"):
            _run("--operator", "ops", "--database", "beautygo")

        assert _untouched(elapsed)

    def test_no_operator_is_a_refusal(self, elapsed):
        with pytest.raises(CommandError, match="--operator is required"):
            _run("--database", _db())

        assert _untouched(elapsed)

    def test_named_and_signed_it_closes(self, elapsed):
        _run("--operator", "ops", "--database", _db())

        elapsed.refresh_from_db()
        assert elapsed.status == Appointment.Status.COMPLETED


class TestB2TheDryRunIsWhatItWas:
    def test_it_needs_neither_key_and_writes_nothing(self, elapsed):
        out = _run("--dry-run")

        assert "would close" in out
        assert _untouched(elapsed)

    def test_a_wrong_name_is_still_heard(self, elapsed):
        with pytest.raises(CommandError, match="is not the database"):
            _run("--dry-run", "--database", "beautygo")


class TestB3TheReceipt:
    def test_dry_and_real_runs_are_tellable(self, elapsed):
        _run("--dry-run")
        _run("--operator", "ops", "--database", _db())

        dry, real = (row.payload for row in _receipts())
        assert (dry["mode"], dry["operator"], dry["result"]) == ("dry_run", None, None)
        assert (real["mode"], real["operator"]) == ("apply", "ops")
        assert real["result"] == {"completed": 1, "skipped": 0, "failed": 0}
        for payload in (dry, real):
            assert payload["database"] == _db()
            assert (payload["matched"], payload["planned"]) == (1, 1)
            assert payload["provider_mode"] == money_commands.provider_mode()

    def test_the_cap_is_in_the_receipt(self, elapsed, client_user, specialist, service):  # noqa: F811
        _booking(client_user, specialist, service, ended_hours_ago=24 * 8)

        _run("--dry-run", "--limit", "1")

        (row,) = _receipts()
        assert (row.payload["matched"], row.payload["planned"], row.payload["limit"]) == (2, 1, 1)


class TestB4ReceiptBeforeTheClosure:
    def test_the_receipt_exists_when_the_sweep_starts(self, elapsed):
        seen: list[int] = []

        def _sweep(**_kwargs):
            seen.append(len(_receipts()))
            return {"completed": 0, "skipped": 0, "failed": 0}

        with patch(SWEEP, side_effect=_sweep):
            _run("--operator", "ops", "--database", _db())

        assert seen == [1]

    def test_no_receipt_nothing_closed(self, elapsed):
        with (
            patch(SWEEP) as sweep,
            patch(RECEIPT, side_effect=RuntimeError("db gone")),
            pytest.raises(money_commands.ReceiptUnwritten, match="NOTHING was closed"),
        ):
            _run("--operator", "ops", "--database", _db())

        sweep.assert_not_called()
        assert _untouched(elapsed)
