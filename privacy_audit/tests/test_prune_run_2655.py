"""DRF-2655 — чистка журнала доступа оставляет квитанцию вне очищаемого журнала.

Пары, которые обязаны различаться:

* чистка прошла (``--apply``) → строка ``PruneRun`` со счётчиками; не было
  чистки (сухой прогон) → квитанции нет и не удалено ничего;
* квитанция записалась → удаление состоялось; не записалась → не удалено
  НИЧЕГО (одна транзакция). Прецеденты ``*_PURGE_RUN`` удаление при сбое
  квитанции не отменяют — для журнала доступа это как раз и есть дыра.

Узел «чистка удалила строки» закрепил бы дефект: удаляла она и раньше.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from privacy_audit.models import PersonalDataAccessLog, PruneRun
from privacy_audit.retention import DEFAULT_RETENTION_DAYS, prune_expired
from privacy_audit.tests.test_retention_1782 import _row

pytestmark = pytest.mark.django_db


@pytest.fixture
def journal():
    return {"fresh": _row(1), "expired": _row(DEFAULT_RETENTION_DAYS + 1)}


class TestACleanupLeavesItsReceipt:
    def test_an_applied_prune_leaves_a_receipt_a_dry_run_leaves_none(self, journal):
        call_command("prune_privacy_audit", "--operator", "owner", stdout=StringIO())
        assert PersonalDataAccessLog.objects.count() == 2  # presence: dry run deleted nothing
        assert PruneRun.objects.count() == 0

        call_command("prune_privacy_audit", "--apply", "--operator", "owner", stdout=StringIO())

        run = PruneRun.objects.get()
        assert (run.operator, run.matched, run.deleted, run.remaining) == ("owner", 1, 1, 1)
        assert run.retention_days == DEFAULT_RETENTION_DAYS
        assert run.oldest_remaining is not None
        assert list(PersonalDataAccessLog.objects.values_list("pk", flat=True)) == [
            journal["fresh"].pk
        ]

    def test_the_receipt_is_not_in_the_journal_it_cleans(self, journal):
        prune_expired(operator="owner")
        assert PruneRun.objects.count() == 1
        # Квитанция не строка журнала доступа: следующая чистка её не видит.
        assert not PersonalDataAccessLog.objects.filter(operator="owner").exists()


class TestNoReceiptNoDeletion:
    def test_a_failed_receipt_rolls_the_deletion_back(self, journal, monkeypatch):
        def refuse(*args, **kwargs):
            raise RuntimeError("simulated receipt failure")

        monkeypatch.setattr(PruneRun.objects, "create", refuse)
        with pytest.raises(RuntimeError):
            prune_expired(operator="owner")

        assert set(PersonalDataAccessLog.objects.values_list("pk", flat=True)) == {
            journal["fresh"].pk, journal["expired"].pk,
        }

    @pytest.mark.parametrize("label", ["", "  ", "x" * 65])
    def test_no_operator_means_nothing_deleted(self, journal, label):
        with pytest.raises(ValueError):
            prune_expired(operator=label)
        assert PersonalDataAccessLog.objects.count() == 2
        assert PruneRun.objects.count() == 0

    def test_the_command_refuses_a_blank_operator(self, journal):
        with pytest.raises(CommandError):
            call_command("prune_privacy_audit", "--apply", "--operator", " ", stdout=StringIO())
        assert PersonalDataAccessLog.objects.count() == 2


class TestTheReceiptOutlivesTheCleanup:
    def test_an_old_receipt_survives_a_later_prune_and_cannot_be_rewritten(self, journal):
        prune_expired(operator="first")
        old = PruneRun.objects.get()
        PruneRun.objects.filter(pk=old.pk)  # queryset exists; its update is forbidden below
        PruneRun._base_manager.filter(pk=old.pk).update(
            occurred_at=timezone.now() - timedelta(days=DEFAULT_RETENTION_DAYS + 10)
        )
        _row(DEFAULT_RETENTION_DAYS + 5)
        prune_expired(operator="second")

        assert set(PruneRun.objects.values_list("operator", flat=True)) == {"first", "second"}
        with pytest.raises(NotImplementedError):
            PruneRun.objects.filter(pk=old.pk).delete()
        with pytest.raises(NotImplementedError):
            PruneRun.objects.filter(pk=old.pk).update(operator="rewritten")
        old.refresh_from_db()
        with pytest.raises(NotImplementedError):
            old.save()
        with pytest.raises(NotImplementedError):
            old.delete()
        assert uuid.UUID(str(old.pk))  # still there
