"""DRF-2655 — карточка оператора пишет журнал §96 и не печатается без него (§107).

``identity_card`` — «просмотр сотрудником чувствительных профилей»: оператор
видит контекст, цели, дневник. До этой правки доступ был, а журнал его не
видел. Пары, которые обязаны различаться:

* прочитал персданные → строка на КАЖДОГО человека карточки (прокси и
  реальный аккаунт); не прочитал (прокси нет — карточки нет) → строк нет;
* журнал доступен → карточка напечатана; недоступен → не напечатана ничего
  (fail-closed §107), а не «напечатана и потеряна громко».

Узел «команда работает» закрепил бы дефект: она работала и без журнала.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from privacy_audit.models import PersonalDataAccessLog
from users.tests.test_identity_card import CID, PROXY, _bound_to_real, _proxy_with_history

pytestmark = pytest.mark.django_db

_Log = PersonalDataAccessLog


def _card(*accounts: str, operator: str = "поддержка") -> str:
    out = io.StringIO()
    args = []
    for account in accounts:
        args += ["--account", account]
    call_command("identity_card", *args, "--operator", operator, stdout=out)
    return out.getvalue()


class TestEveryPersonOnTheCardIsJournalled:
    def test_a_card_read_writes_one_row_per_person_an_absent_card_writes_none(self):
        proxy = _proxy_with_history()
        real = _bound_to_real(proxy)

        _card(f"max:{CID}", "max:404404404")  # вторая — прокси нет, карточки нет

        rows = list(
            _Log.objects.order_by("object_id").values_list(
                "object_id", "caller_purpose", "actor_id", "actor_role", "operator",
                "operation", "object_category", "result",
            )
        )
        expected = sorted(
            [
                (who.pk, _Log.CallerPurpose.OPERATOR_COMMAND, None, "operator", "поддержка",
                 _Log.Operation.OPERATOR_CARD_READ, _Log.ObjectCategory.PERSONAL_DATA,
                 _Log.Result.ALLOWED)
                for who in (proxy, real)
            ]
        )
        # Ровно две строки — прочитанные люди; несуществующий прокси строки не дал.
        assert rows == expected

    def test_the_operator_is_not_the_actor(self):
        _proxy_with_history()
        _card(f"max:{CID}", operator="  дежурный  ")
        (row,) = _Log.objects.values("actor_id", "actor_named", "operator")
        # Кто назвался — в operator (после strip); разрешённого актора нет.
        assert row == {"actor_id": None, "actor_named": False, "operator": "дежурный"}


class TestNoJournalNoCard:
    def test_the_card_prints_when_journalled_and_not_at_all_when_the_journal_is_down(
        self, monkeypatch
    ):
        _proxy_with_history()
        printed = _card(f"max:{CID}")
        assert PROXY in printed

        from privacy_audit.services import AuditUnavailable

        def down(**kwargs):
            raise AuditUnavailable("simulated journal outage")

        monkeypatch.setattr("privacy_audit.services.record_access", down)
        out = io.StringIO()
        with pytest.raises(CommandError, match="карточка не печатается"):
            call_command(
                "identity_card", "--account", f"max:{CID}", "--operator", "поддержка", stdout=out
            )
        assert PROXY not in out.getvalue()


class TestTheOperatorLabel:
    @pytest.mark.parametrize("label", ["", "   ", "x" * 65])
    def test_an_empty_or_overlong_label_refuses_before_reading(self, label):
        _proxy_with_history()
        before = _Log.objects.count()
        with pytest.raises(CommandError):
            _card(f"max:{CID}", operator=label)
        assert _Log.objects.count() == before

    def test_a_label_at_the_limit_is_kept_whole(self):
        _proxy_with_history()
        _card(f"max:{CID}", operator="x" * 64)
        assert _Log.objects.get().operator == "x" * 64
