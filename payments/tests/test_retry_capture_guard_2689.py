"""DRF-2689 — ``retry_capture`` charges only when told to, where told to, on the record.

A capture is a charge to the client and cannot be taken back. Before this
ticket the command captured on invocation, on whatever database the shell
was connected to, and left nothing behind but stdout. Nodes come in pairs
that must differ:

* g1 — without ``--apply`` nothing is dispatched and nothing changes; with
  ``--apply`` on the named database the same payment is captured;
* g2 — ``--apply`` without ``--database``, or naming another database, is a
  loud refusal before the first capture; a wrong name is refused on a dry
  run too;
* g3 — every run leaves one receipt; dry and real are tellable; it carries
  payment ids and a total, never the provider key;
* g4 — the receipt of a real run exists BEFORE the first capture, and a
  receipt that cannot be written means nothing is captured; on a dry run
  the same failure does not fail the run;
* g5 — ``--operator`` is required, non-empty, not truncated.

The database name is read here from the live connection, not from the helper
under test.
"""
from __future__ import annotations

import json
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import CommandError, call_command
from django.db import connection

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from payments import money_commands
from payments.models import Payment
from payments.tests.test_capture_flow import (  # noqa: F401 — fixtures
    _held_payment,
    _make_appointment,
    _svc_with_mocked_sdk,
    category,
    client_user,
    service,
    specialist_user,
)

pytestmark = pytest.mark.django_db

NAME = event_catalogue.RETRY_CAPTURE_RUN
TASK = "payments.management.commands.retry_capture.capture_payment_task"
RECEIPT = "payments.management.commands.retry_capture.write_receipt"


def _db() -> str:
    return connection.settings_dict["NAME"]


def _run(*args: str, operator: str | None = "ops") -> str:
    out, err = StringIO(), StringIO()
    head = ["--operator", operator] if operator is not None else []
    call_command("retry_capture", *head, *args, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


def _receipts() -> list[AnalyticsEvent]:
    return list(AnalyticsEvent.objects.filter(event_name=NAME).order_by("created_at"))


@pytest.fixture
def stuck(client_user, specialist_user, service) -> Payment:  # noqa: F811
    appt = _make_appointment(client_user, specialist_user, service, "completed")
    payment = _held_payment(appt)
    payment.capture_state = Payment.CaptureState.CAPTURE_FAILED
    payment.save()
    return payment


@pytest.fixture
def sdk():
    svc, sdk_payment = _svc_with_mocked_sdk()
    with patch("payments.services.YooKassaService", return_value=svc):
        yield sdk_payment


def _untouched(payment: Payment) -> bool:
    payment.refresh_from_db()
    return (payment.status, payment.capture_state) == (
        Payment.Status.AUTHORIZED,
        Payment.CaptureState.CAPTURE_FAILED,
    )


def test_the_test_database_is_what_this_file_names():
    """Positive control for ``_db``: pytest-django's database, by its literal prefix."""
    assert _db().startswith("test_")
    assert money_commands.database_name() == _db()


class TestG1NothingWithoutApply:
    def test_a_dry_run_dispatches_no_capture(self, stuck, sdk):
        with patch(TASK) as task:
            out = _run()

        task.assert_not_called()
        task.apply_async.assert_not_called()
        sdk.capture.assert_not_called()
        assert _untouched(stuck)
        assert f"would capture: payment_id={stuck.id}" in out
        assert f"database={_db()}" in out
        assert "dry run: nothing captured" in out

    def test_a_sync_flag_alone_captures_nothing(self, stuck, sdk):
        """``--sync`` was the whole old contract; it is not ``--apply``."""
        _run("--sync")

        sdk.capture.assert_not_called()
        assert _untouched(stuck)

    def test_apply_on_the_named_database_captures(self, stuck, sdk):
        _run("--database", _db(), "--apply", "--sync")

        stuck.refresh_from_db()
        assert stuck.status == Payment.Status.PAID
        assert stuck.capture_state == Payment.CaptureState.CAPTURED_PENDING_SETTLEMENT
        assert sdk.capture.call_count == 1

    def test_apply_without_sync_enqueues(self, stuck):
        with patch(TASK) as task:
            _run("--database", _db(), "--apply")

        task.assert_not_called()
        task.apply_async.assert_called_once_with(args=[str(stuck.id)])


class TestG2TheDatabaseIsNamed:
    def test_apply_without_a_database_is_refused(self, stuck, sdk):
        with patch(TASK) as task, pytest.raises(CommandError, match="--database is required"):
            _run("--apply", "--sync")

        task.assert_not_called()
        sdk.capture.assert_not_called()
        assert _untouched(stuck)
        assert _receipts() == []

    def test_apply_naming_another_database_is_refused(self, stuck, sdk):
        with patch(TASK) as task, pytest.raises(CommandError) as refusal:
            _run("--database", "beautygo", "--apply", "--sync")

        assert "'beautygo'" in str(refusal.value) and repr(_db()) in str(refusal.value)
        task.assert_not_called()
        sdk.capture.assert_not_called()
        assert _untouched(stuck)

    def test_a_wrong_name_is_refused_on_a_dry_run_too(self, stuck):
        with pytest.raises(CommandError, match="is not the database"):
            _run("--database", "beautygo")


class TestG3TheReceipt:
    def test_dry_and_real_runs_are_tellable(self, stuck, sdk):
        _run()
        _run("--database", _db(), "--apply", "--sync")

        dry, real = (row.payload for row in _receipts())
        assert (dry["mode"], real["mode"]) == ("dry_run", "apply")
        for payload in (dry, real):
            assert payload["operator"] == "ops"
            assert payload["database"] == _db()
            assert payload["matched"] == 1
            assert payload["payment_ids"] == [str(stuck.id)]
            assert Decimal(payload["amount_total"]) == stuck.amount
        assert (dry["dispatch"], real["dispatch"]) == ("async", "sync")
        assert (dry["scope"], real["scope"]) == ("all", "all")

    def test_the_total_is_the_sum_and_one_payment_can_be_named(
        self, stuck, client_user, specialist_user, service,  # noqa: F811
    ):
        other = _held_payment(
            _make_appointment(client_user, specialist_user, service, "completed")
        )
        Payment.objects.filter(pk=other.pk).update(
            provider_payment_id="yk_hold_002", capture_scheduled_for=None,
        )

        _run()
        _run("--payment-id", str(other.id))

        everything, single = (row.payload for row in _receipts())
        assert everything["matched"] == 2
        assert Decimal(everything["amount_total"]) == stuck.amount + other.amount
        assert set(everything["payment_ids"]) == {str(stuck.id), str(other.id)}
        assert (single["scope"], single["payment_ids"]) == ("single", [str(other.id)])

    @pytest.mark.parametrize(
        ("key", "mode"),
        [("", "unset"), ("test_k3y", "test"), ("live_k3y", "live"), ("k3y", "unknown")],
    )
    def test_the_provider_mode_is_recorded_and_the_key_is_not(
        self, stuck, settings, key, mode,
    ):
        settings.YOOKASSA_SECRET_KEY = key

        out = _run()

        (row,) = _receipts()
        assert row.payload["provider_mode"] == mode
        assert f"provider={mode}" in out
        assert "k3y" not in json.dumps(row.payload) and "k3y" not in out

    def test_a_receipt_is_a_server_row(self, stuck):
        _run()

        (row,) = _receipts()
        assert (row.actor_id, row.anonymous_session_id) == (None, None)


class TestG4ReceiptBeforeTheCharge:
    def test_the_receipt_exists_when_the_first_capture_starts(self, stuck):
        seen: list[int] = []
        task = MagicMock(side_effect=lambda _id: seen.append(len(_receipts())))

        with patch(TASK, task):
            _run("--database", _db(), "--apply", "--sync")

        assert seen == [1]

    def test_no_receipt_no_capture(self, stuck, sdk):
        with (
            patch(TASK) as task,
            patch(RECEIPT, side_effect=RuntimeError("db gone")),
            pytest.raises(money_commands.ReceiptUnwritten, match="NOTHING was captured"),
        ):
            _run("--database", _db(), "--apply", "--sync")

        task.assert_not_called()
        task.apply_async.assert_not_called()
        sdk.capture.assert_not_called()
        assert _untouched(stuck)

    def test_a_dry_run_survives_its_receipt(self, stuck):
        with patch(RECEIPT, side_effect=RuntimeError("db gone")):
            out = _run()

        assert "dry-run receipt not written" in out
        assert "dry run: nothing captured" in out


class TestG5TheOperator:
    def test_it_is_required(self, stuck):
        with pytest.raises(CommandError):
            _run(operator=None)
        assert _receipts() == []

    def test_it_is_not_blank(self, stuck):
        with pytest.raises(CommandError, match="--operator is required"):
            _run(operator="   ")

    def test_it_is_not_truncated(self, stuck):
        with pytest.raises(CommandError, match="not truncated"):
            _run(operator="x" * 65)
        assert _receipts() == []
