"""Ретенция журнала доступа к персданным (DRF-1782, §96).

Единственный путь удаления — ``privacy_audit.prune_expired`` →
``prune_before``; период — параметр с умолчанием, не решение в коде;
кривая настройка — отказ и ноль удалённых, не «удалить всё».
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

import privacy_audit
from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.retention import (
    DEFAULT_RETENTION_DAYS,
    RetentionMisconfigured,
    prune_expired,
    retention_days,
)

pytestmark = pytest.mark.django_db


def _row(age_days: int) -> PersonalDataAccessLog:
    """Строка журнала возрастом ``age_days``. ``occurred_at`` — auto_now_add,
    поэтому возраст ставится обходом ORM-запретов через ``update`` на pk:
    это тест, и он это говорит; приложение так не пишет."""
    row = PersonalDataAccessLog.objects.create(
        caller_purpose=PersonalDataAccessLog.CallerPurpose.INTERNAL,
        actor=None,
        actor_role="service",
        actor_named=False,
        object_id=uuid.uuid4(),
        operation=PersonalDataAccessLog.Operation.READ_CONTEXT,
        object_category=PersonalDataAccessLog.ObjectCategory.PERSONAL_CONTEXT,
        result=PersonalDataAccessLog.Result.ALLOWED,
    )
    PersonalDataAccessLog.objects.filter(pk=row.pk).update(
        occurred_at=timezone.now() - timedelta(days=age_days)
    )
    return row


@pytest.fixture
def journal():
    """Три строки: свежая, на границе года и заведомо старше."""
    return {
        "fresh": _row(1),
        "inside": _row(DEFAULT_RETENTION_DAYS - 1),
        "expired": _row(DEFAULT_RETENTION_DAYS + 1),
        "ancient": _row(DEFAULT_RETENTION_DAYS + 400),
    }


class TestPeriodIsAParameter:
    def test_default_is_the_provisional_year(self, settings):
        delattr(settings, "PRIVACY_AUDIT_RETENTION_DAYS")
        assert retention_days() == 365 == DEFAULT_RETENTION_DAYS

    def test_setting_wins_and_may_be_a_string_from_env(self, settings):
        settings.PRIVACY_AUDIT_RETENTION_DAYS = "30"
        assert retention_days() == 30
        settings.PRIVACY_AUDIT_RETENTION_DAYS = 1095
        assert retention_days() == 1095

    @pytest.mark.parametrize("bad", ["", "год", 0, -5, None])
    def test_misconfiguration_refuses_and_deletes_nothing(self, settings, journal, bad):
        settings.PRIVACY_AUDIT_RETENTION_DAYS = bad
        with pytest.raises(RetentionMisconfigured):
            retention_days()
        with pytest.raises(RetentionMisconfigured):
            prune_expired()
        assert PersonalDataAccessLog.objects.count() == 4


class TestPruneExpired:
    def test_deletes_only_rows_older_than_the_period(self, journal):
        out = prune_expired()
        assert (out.matched, out.deleted, out.remaining) == (2, 2, 2)
        assert not out.dry_run
        left = set(PersonalDataAccessLog.objects.values_list("pk", flat=True))
        assert left == {journal["fresh"].pk, journal["inside"].pk}
        assert out.oldest_remaining == PersonalDataAccessLog.objects.get(
            pk=journal["inside"].pk
        ).occurred_at
        # Повтор — нечего удалять, и это не ошибка.
        again = prune_expired()
        assert (again.matched, again.deleted, again.remaining) == (0, 0, 2)

    def test_dry_run_counts_and_deletes_nothing(self, journal):
        out = prune_expired(dry_run=True)
        assert out.dry_run and out.matched == 2 and out.deleted == 0
        assert PersonalDataAccessLog.objects.count() == 4

    def test_a_shorter_period_reaches_more_rows(self, settings, journal):
        settings.PRIVACY_AUDIT_RETENTION_DAYS = 2
        out = prune_expired()
        assert out.deleted == 3
        assert list(PersonalDataAccessLog.objects.values_list("pk", flat=True)) == [journal["fresh"].pk]

    def test_app_level_alias_is_the_same_path(self, journal):
        out = privacy_audit.prune_expired(dry_run=True)
        assert out.matched == 2


class TestAppendOnlyStillHolds:
    def test_orm_delete_is_still_refused_everywhere_else(self, journal):
        with pytest.raises(NotImplementedError):
            PersonalDataAccessLog.objects.all().delete()
        with pytest.raises(NotImplementedError):
            PersonalDataAccessLog.objects.filter(pk=journal["ancient"].pk).delete()
        with pytest.raises(NotImplementedError):
            journal["ancient"].delete()
        assert PersonalDataAccessLog.objects.count() == 4

    def test_prune_before_cannot_be_widened_past_its_cutoff(self, journal):
        """Набор без фильтра + граница «в будущем» удалил бы всё; метод
        применяет границу сам, а не доверяет набору."""
        cutoff = timezone.now() - timedelta(days=DEFAULT_RETENTION_DAYS)
        n = PersonalDataAccessLog.objects.all().prune_before(cutoff)
        assert n == 2
        assert PersonalDataAccessLog.objects.count() == 2


class TestCommand:
    def test_default_is_dry_run_with_the_subject_printed(self, journal):
        out = StringIO()
        call_command("prune_privacy_audit", stdout=out)
        text = out.getvalue()
        assert "хост (hostname процесса)" in text
        assert "период (PRIVACY_AUDIT_RETENTION_DAYS) : 365 дн" in text
        assert "строк старше границы       : 2" in text
        assert "удалено                    : 0  (сухой прогон" in text
        assert "осталось строк             : 4" in text
        assert PersonalDataAccessLog.objects.count() == 4

    def test_apply_deletes_and_prints_the_oldest_remaining(self, journal):
        out = StringIO()
        call_command("prune_privacy_audit", "--apply", stdout=out)
        text = out.getvalue()
        assert "удалено                    : 2" in text
        assert "осталось строк             : 2" in text
        oldest = PersonalDataAccessLog.objects.get(pk=journal["inside"].pk).occurred_at
        assert oldest.isoformat() in text
        assert PersonalDataAccessLog.objects.count() == 2

    def test_misconfigured_period_is_a_command_error_and_nothing_is_deleted(self, settings, journal):
        settings.PRIVACY_AUDIT_RETENTION_DAYS = "0"
        with pytest.raises(CommandError, match="ничего не удалено"):
            call_command("prune_privacy_audit", "--apply", stdout=StringIO())
        assert PersonalDataAccessLog.objects.count() == 4
