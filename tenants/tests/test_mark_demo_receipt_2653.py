"""DRF-2653 — у запуска пометки демо остаётся след, и сухой прогон отличим от записи.

Причину DRF-2646 («is_demo у всех false») пришлось устанавливать исключением:
«не запускали» и «запускали только сухим прогоном» следа не оставляли. Узлы —
парами, которые обязаны различаться:

* r1 — сухой прогон оставляет квитанцию «проверено, не применено», запись —
  «применено» с числом; одна квитанция на запуск;
* r2 — ``changed`` считается по базе, а не копирует ``matched``: план,
  разошедшийся с базой, даёт matched 1 / changed 0;
* r3 — fail-closed на ``--apply``: квитанция не записалась — пометка откатана;
  на сухом прогоне тот же сбой прогон не роняет;
* r4 — ``--operator`` обязателен и не пуст;
* r5 — в квитанции слаги и id личностей, не username.
"""
from __future__ import annotations

import json
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from tenants.management.commands import mark_demo_and_test_personas as cmd
from tenants.models import Tenant

User = get_user_model()

pytestmark = pytest.mark.django_db

SLUG = "mark2653-demo"
NAME = event_catalogue.MARK_DEMO_AND_TEST_PERSONAS_RUN


def _run(*args, operator: str = "owner") -> str:
    out, err = StringIO(), StringIO()
    call_command("mark_demo_and_test_personas", "--operator", operator, *args, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


def _receipts() -> list[AnalyticsEvent]:
    return list(AnalyticsEvent.objects.filter(event_name=NAME).order_by("created_at"))


@pytest.fixture
def demo_salon() -> Tenant:
    return Tenant.objects.create(slug=SLUG, name="Демо", is_active=True)


@pytest.fixture
def persona() -> User:
    return User.objects.create_user(
        username="mark2653_owner", password="x", role="client", phone="+79992426531",
    )


class TestR1DryRunAndApplyAreTellable:
    def test_a_dry_run_leaves_a_checked_not_applied_receipt(self, demo_salon, persona):
        _run("--slug", SLUG, "--persona", persona.username)

        [receipt] = _receipts()
        p = receipt.payload
        assert p["mode"] == "dry_run"
        assert p["direction"] == "mark"
        assert p["operator"] == "owner"
        assert p["salons"]["to_change"] == [SLUG]
        assert p["salons"]["matched"] == 1
        assert p["salons"]["changed"] is None
        assert p["personas"]["changed"] is None
        # Серверная квитанция: ни актора, ни анонимной сессии (клиентская их несёт).
        assert receipt.actor_id is None and receipt.anonymous_session_id is None
        demo_salon.refresh_from_db()
        assert demo_salon.is_demo is False

    def test_apply_leaves_an_applied_receipt_with_the_count(self, demo_salon, persona):
        _run("--slug", SLUG, "--persona", persona.username, "--apply")

        [receipt] = _receipts()
        p = receipt.payload
        assert p["mode"] == "apply"
        assert p["salons"]["matched"] == 1
        assert p["salons"]["changed"] == 1
        assert p["personas"]["matched"] == 1
        assert p["personas"]["changed"] == 1

    def test_each_run_is_one_receipt_and_unmark_says_so(self, demo_salon):
        _run("--slug", SLUG, "--apply")
        _run("--unmark", "--slug", SLUG)
        _run("--unmark", "--slug", SLUG, "--apply")

        # Без порядка: created_at трёх быстрых запусков совпадает на часах Windows.
        got = sorted((r.payload["mode"], r.payload["direction"]) for r in _receipts())
        assert got == sorted([("apply", "mark"), ("dry_run", "unmark"), ("apply", "unmark")])


class TestR2ChangedIsCountedNotCopied:
    def test_a_plan_that_diverged_from_the_base_shows_matched_1_changed_0(
        self, demo_salon, monkeypatch
    ):
        # План говорит «пометить», а в базе признак уже стоит (кто-то успел
        # между разбором и записью). Число «изменено» обязано это показать.
        real = cmd.Command._classify

        def stale_plan(self, slugs, personas, *, unmark=False):
            plan = real(self, slugs, personas, unmark=unmark)
            Tenant.all_objects.filter(slug=SLUG).update(is_demo=True)
            return plan

        monkeypatch.setattr(cmd.Command, "_classify", stale_plan)

        _run("--slug", SLUG, "--apply")

        [receipt] = _receipts()
        assert receipt.payload["salons"]["matched"] == 1
        assert receipt.payload["salons"]["changed"] == 0


class TestR3FailClosedOnApply:
    @pytest.fixture
    def unwritable_receipt(self, monkeypatch):
        # Настоящий отказ базы, а не заглушка: имя длиннее колонки (64).
        monkeypatch.setattr(event_catalogue, "MARK_DEMO_AND_TEST_PERSONAS_RUN", "x" * 65)

    def test_apply_without_a_receipt_changes_nothing(
        self, demo_salon, persona, unwritable_receipt
    ):
        with pytest.raises(cmd.ReceiptUnwritten, match="НЕ применена"):
            _run("--slug", SLUG, "--persona", persona.username, "--apply")

        demo_salon.refresh_from_db()
        persona.refresh_from_db()
        assert demo_salon.is_demo is False
        assert persona.is_test_persona is False

    def test_a_dry_run_survives_the_same_failure(self, demo_salon, unwritable_receipt):
        text = _run("--slug", SLUG)

        assert "квитанция сухого прогона не записана" in text
        assert "сухой прогон: ничего не записано" in text


class TestR4OperatorIsRequired:
    def test_missing_operator_is_refused(self, demo_salon):
        with pytest.raises(CommandError, match="operator"):
            call_command("mark_demo_and_test_personas", "--slug", SLUG, stdout=StringIO())
        assert _receipts() == []  # empty-assert-ok: пара — r1, где квитанция есть

    def test_blank_operator_is_refused(self, demo_salon):
        with pytest.raises(CommandError, match="--operator пуст"):
            _run("--slug", SLUG, "--apply", operator="   ")
        demo_salon.refresh_from_db()
        assert demo_salon.is_demo is False


class TestR5NoNamesInTheReceipt:
    def test_personas_go_by_id_and_unknown_names_by_count(self, demo_salon, persona):
        _run("--slug", SLUG, "--persona", persona.username, "--persona", "нет-такого")

        [receipt] = _receipts()
        dumped = json.dumps(receipt.payload, ensure_ascii=False)
        assert str(persona.pk) in dumped
        assert persona.username not in dumped
        assert "нет-такого" not in dumped
        assert receipt.payload["personas"]["absent_count"] == 1
