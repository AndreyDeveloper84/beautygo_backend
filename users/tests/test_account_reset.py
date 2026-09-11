"""B-R — the account reset command, catalog half (DRF-1617).

The test that matters is the red line on a NON-empty account: completeness
is red before the reset and green after. A green check on an account with
no traces proves nothing and is not here.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from billing.models import SpecialistSubscription, TariffPlan
from goals.models import ClientGoal, GoalAnketaRun
from users import account_reset as reset
from users.models import User, UserPersonalContext
from users.services import bind_external_identity, resolve_external_user

pytestmark = pytest.mark.django_db

ACCOUNT = "max:999000111"
PROXY = "bot:max:999000111"


def _proxy_with_history() -> User:
    """A bot identity that has been through onboarding: declared preferences
    on the proxy and a completed questionnaire (goal + run, PROTECT)."""
    proxy = resolve_external_user(PROXY)
    UserPersonalContext.objects.create(user=proxy, diet_type="vegan")
    ClientGoal.objects.create(client=proxy, goal_key="relax", source_channel="bot")
    GoalAnketaRun.objects.create(client=proxy)
    return proxy


def _bound_to_real(proxy: User) -> User:
    real = User.objects.create_user(
        username="real-test-person", password="x", role="client", phone="+79990001122"
    )
    bind_external_identity(PROXY, real.id)
    proxy.refresh_from_db()
    assert proxy.linked_user_id == real.id
    return real


def _run(*args: str) -> str:
    out = io.StringIO()
    call_command("reset_test_account", *args, stdout=out)
    return out.getvalue()


# --- the red line ------------------------------------------------------------


class TestTheRedLineOnANonEmptyAccount:
    def test_completeness_is_red_before_and_green_after(self, settings):
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT]
        proxy = _proxy_with_history()
        ids = [proxy.id]

        before = {lo.label for lo in reset.verify(ids)}
        assert before >= {
            "users.User",
            "users.UserPersonalContext.user",
            "goals.ClientGoal.client",
            "goals.GoalAnketaRun.client",
        }
        assert reset.verify(ids) != []

        report = reset.apply(ACCOUNT, "client-onboarding")

        assert report.leftovers == []
        assert reset.verify(ids) == []
        assert not User.objects.filter(username=PROXY).exists()
        assert report.removed["goals.GoalAnketaRun"] == 1
        assert report.removed["users.UserPersonalContext"] == 1

    def test_the_next_resolve_is_a_new_person(self, settings):
        """The measured half-reset: bot-only reset left this proxy, and
        ``resolve_external_user`` handed the OLD catalog id back. After the
        catalog half the same header resolves to a fresh row."""
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT]
        old = _proxy_with_history()
        reset.apply(ACCOUNT, "client-onboarding")
        fresh = resolve_external_user(PROXY)
        assert fresh.id != old.id
        assert fresh.linked_user_id is None


# --- the binding is followed, and the real account has to be listed itself -


class TestTheBoundRealAccount:
    def test_proxy_listed_real_not_listed_refuses_and_names_it(self, settings):
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT]
        proxy = _proxy_with_history()
        real = _bound_to_real(proxy)

        p = reset.plan(ACCOUNT, "client-onboarding")
        assert [s.kind for s in p.subjects] == ["proxy", "real"]
        assert [s.listed_as for s in p.unlisted] == [str(real.id)]

        with pytest.raises(reset.NotAllowed) as exc:
            reset.apply(ACCOUNT, "client-onboarding")
        assert str(real.id) in str(exc.value)
        assert User.objects.filter(id__in=[proxy.id, real.id]).count() == 2

    def test_both_listed_frees_both_and_the_phone(self, settings):
        proxy = _proxy_with_history()
        real = _bound_to_real(proxy)
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT, str(real.id)]

        assert reset.verify([proxy.id, real.id]) != []
        report = reset.apply(ACCOUNT, "client-onboarding")

        assert report.leftovers == []
        assert reset.verify([proxy.id, real.id]) == []
        assert not User.objects.filter(phone="+79990001122").exists(), "the phone is free again"

    def test_a_sibling_proxy_of_the_real_account_is_a_subject_too(self, settings):
        proxy = _proxy_with_history()
        real = _bound_to_real(proxy)
        bind_external_identity("bot:telegram:555", real.id)
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT, str(real.id)]

        p = reset.plan(ACCOUNT, "client-onboarding")
        assert [(s.kind, s.listed_as) for s in p.subjects] == [
            ("proxy", ACCOUNT),
            ("real", str(real.id)),
            ("proxy-sibling", "telegram:555"),
        ]
        assert [s.listed_as for s in p.unlisted] == ["telegram:555"]
        with pytest.raises(reset.NotAllowed):
            reset.apply(ACCOUNT, "client-onboarding")


# --- the wall ----------------------------------------------------------------


class TestTheAllowlistIsAWallNotAPrompt:
    def test_empty_list_refuses_everyone(self, settings):
        settings.ACCOUNT_RESET_ALLOWLIST = []
        proxy = _proxy_with_history()
        with pytest.raises(reset.NotAllowed):
            reset.apply(ACCOUNT, "client-onboarding")
        assert User.objects.filter(id=proxy.id).exists()

    def test_the_command_says_why_and_exits_non_zero(self, settings):
        settings.ACCOUNT_RESET_ALLOWLIST = []
        _proxy_with_history()
        out = io.StringIO()
        with pytest.raises(CommandError, match="отказано"):
            call_command(
                "reset_test_account", "--account", ACCOUNT, "--mode", "client-onboarding", "--apply", stdout=out,
            )
        text = out.getvalue()
        assert "ПУСТ" in text
        assert "НЕ В СПИСКЕ" in text
        assert User.objects.filter(username=PROXY).exists()


# --- the dry run is a read ---------------------------------------------------


class TestTheDryRunWritesNothing:
    def test_no_delete_or_update_reaches_the_database(self, settings):
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT]
        proxy = _proxy_with_history()
        with CaptureQueriesContext(connection) as ctx:
            out = _run("--account", ACCOUNT, "--mode", "client-onboarding")
        verbs = [q["sql"].lstrip().split(" ", 1)[0].upper() for q in ctx.captured_queries]
        assert verbs, "the dry run must have READ something"
        assert set(verbs) <= {"SELECT", "SAVEPOINT", "RELEASE"}, sorted(set(verbs))
        assert User.objects.filter(id=proxy.id).exists()
        assert "сухой прогон" in out

    def test_zeros_are_printed_as_outcomes(self, settings):
        _proxy_with_history()
        out = _run("--account", ACCOUNT, "--mode", "client-onboarding")
        for rel in reset.incoming_relations():
            assert rel.label in out, rel.label
        assert "PROTECT, 0 строк — не блокирует" in out
        for label in reset.KEPT_BY_DESIGN:
            assert label in out, label


# --- PROTECT refuses by name ------------------------------------------------


class TestProtectRefusesByName:
    def test_a_subscription_blocks_and_nothing_is_written(self, settings):
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT]
        proxy = _proxy_with_history()
        SpecialistSubscription.objects.create(
            user=proxy,
            tariff=TariffPlan.objects.get(code=TariffPlan.Code.SOLO),
            status=SpecialistSubscription.Status.ACTIVE,
            current_period_start=date(2026, 9, 1),
            current_period_end=date(2026, 9, 30),
        )
        with pytest.raises(reset.Blocked) as exc:
            reset.apply(ACCOUNT, "client-onboarding")
        assert [ln.label for ln in exc.value.lines] == ["billing.SpecialistSubscription.user"]
        assert GoalAnketaRun.objects.filter(client=proxy).exists(), "refused BEFORE the first write"

    def test_the_questionnaire_is_dismantled_by_design_not_bypassed(self):
        for mode in reset.MODES.values():
            assert "goals.GoalAnketaRun.client" in mode.dismantles
            assert "appointments.Appointment.client" not in mode.dismantles
            assert "billing.SpecialistSubscription.user" not in mode.dismantles


# --- completeness inside the transaction ------------------------------------


class TestCompletenessRollsBack:
    def test_a_leftover_rolls_the_whole_reset_back(self, settings, monkeypatch):
        settings.ACCOUNT_RESET_ALLOWLIST = [ACCOUNT]
        proxy = _proxy_with_history()
        real_verify = reset.verify
        monkeypatch.setattr(reset, "verify", lambda ids: real_verify(ids) + [reset.Leftover("x.Y.z", 1)])

        report = reset.apply(ACCOUNT, "client-onboarding")

        assert [lo.label for lo in report.leftovers] == ["x.Y.z"]
        assert User.objects.filter(id=proxy.id).exists(), "rolled back — nothing half-freed"
        assert GoalAnketaRun.objects.filter(client=proxy).exists()


# --- what is kept is named, and the names are checked ------------------------


class TestKeptByDesign:
    def test_every_entry_is_a_real_model_or_says_it_is_missing(self, settings):
        _proxy_with_history()
        for kept in reset.plan(ACCOUNT, "client-onboarding").kept:
            app_label, model_name, column = kept.label.split(".", 2)
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                assert kept.rows is None, f"{kept.label}: model missing but a count was reported"
                continue
            f = model._meta.get_field(column)
            assert not f.is_relation, f"{kept.label} is a real FK — it belongs in the walk"
            assert kept.rows is not None

    def test_a_missing_model_is_said_out_loud(self):
        _proxy_with_history()
        if apps.is_installed("privacy_audit"):
            pytest.skip("privacy_audit is installed — the exclusion now excludes something")
        out = _run("--account", ACCOUNT, "--mode", "client-onboarding")
        assert "модели нет — исключение пока ничего не исключает" in out

    def test_the_walk_sees_hidden_relations(self):
        hidden = [r.label for r in reset.incoming_relations() if r.hidden]
        assert hidden, "no hidden relation at all — the walk dropped include_hidden"
        assert "services.ServiceTemplate.approved_by" in hidden

    def test_every_mode_dismantles_only_relations_that_exist_and_are_protect(self):
        by_label = {r.label: r for r in reset.incoming_relations()}
        for mode in reset.MODES.values():
            for label in mode.dismantles:
                assert label in by_label, f"{mode.name} names a relation that is not there: {label}"
                assert by_label[label].on_delete == "PROTECT", label


# --- the account spec --------------------------------------------------------


class TestTheAccountSpec:
    @pytest.mark.parametrize("bad", ["83146139", "max:", ":1", ""])
    def test_malformed_is_refused(self, bad):
        with pytest.raises(ValueError):
            reset.parse_account(bad)

    def test_same_spelling_as_the_bot_half(self):
        assert reset.proxy_username("max:83146139") == "bot:max:83146139"

    def test_unknown_account_is_a_named_zero(self, settings):
        settings.ACCOUNT_RESET_ALLOWLIST = ["max:404"]
        out = _run("--account", "max:404", "--mode", "client-onboarding", "--apply")
        assert "0 строк users.User" in out

    def test_a_real_account_cannot_be_named_directly(self):
        """The command takes bot identities only: a bare UUID is not an
        account spec. A real account is reached through its proxy and has to
        be listed — it cannot be the thing you point at."""
        with pytest.raises(ValueError):
            reset.parse_account(str(uuid.uuid4()))
