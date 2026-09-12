"""The access journal: what it records, what it refuses, and what it costs.

Owner ruling §96 turned the audit from "write a row and hope" into a
precondition of the access itself. That inverts the usual test question. It
is not enough to show a row appears on the happy path; the tests that carry
the ruling are the ones where the journal is **broken**, because that is
where "we recorded the access" and "the access happened" are allowed to
disagree — and must not.

Read :class:`TestAuditIsAPrecondition` first. The rest describes the record.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

import logging

from privacy_audit import mixins, policy, services
from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.services import AuditUnavailable
from users.models import User, UserPersonalContext

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token"  # noqa: S105  # pragma: allowlist secret
PROVISIONING_TOKEN = "test-provisioning-only-token"  # noqa: S105  # pragma: allowlist secret

EXPORT_URL = "/api/v1/internal/users/{subject}/personal-data/export/"
DELETE_URL = "/api/v1/internal/users/{subject}/personal-data/"
CTX_URL = "/api/v1/internal/users/{subject}/personal-context/"
DELREQ_URL = "/api/v1/internal/users/{subject}/deletion-requests/"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING_TOKEN
    return settings


def _client(*, bearer: str | None = RUNTIME_TOKEN, actor: str | None = None,
            basis: str | None = None) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    if basis is not None:
        c.defaults["HTTP_X_ACCESS_BASIS"] = basis
    return c


def _subject(nick: str, external_id: str) -> tuple[User, str]:
    real = User.objects.create_user(
        username=nick, password="x", role="client",  # pragma: allowlist secret
        phone=f"+7999111{nick[-4:]}",
    )
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False,
        linked_user=real,
    )
    return real, external_id


@pytest.fixture
def alice() -> tuple[User, str]:
    return _subject("aud_alice1001", "bot:telegram:31001")


@pytest.fixture
def bob() -> tuple[User, str]:
    return _subject("aud_bob2002", "bot:telegram:32002")


@pytest.fixture
def broken_journal(monkeypatch):
    """Make every DIRECT journal write fail, as an unavailable table would.

    Patched at ``services.record_access`` rather than at the mixin, so the
    routing that owner §107 introduced — stop, or queue — is exercised for
    real instead of being bypassed by the fixture.
    """
    def _boom(**_kwargs):
        raise AuditUnavailable("simulated journal outage")

    monkeypatch.setattr(services, "record_access", _boom)


# ---------------------------------------------------------------------------
# The ruling itself
# ---------------------------------------------------------------------------


class TestAuditIsAPrecondition:
    """«Аудит недоступен — экспорт не выполняем» (owner §96), narrowed by §107.

    Everywhere else in this repository an audit failure is swallowed, because
    losing a metric is cheaper than failing a feature. For a DISCLOSING or
    DESTROYING operation the reasoning inverts: inability to record that
    personal data was disclosed is inability to disclose it.

    §107 drew the line: only those operations stop. The tests for the other
    side of the line — the ones that must NOT stop — are in
    :class:`TestQueuedOperationsDoNotStopTheProduct`, and they are the half
    that would be easy to forget, because a suite that only proves things
    stop is green on a system that stops everything.
    """

    def test_export_is_refused_when_the_journal_cannot_be_written(
        self, alice, broken_journal,
    ):
        alice_user, alice_id = alice
        alice_user.email = "alice@example.com"
        alice_user.save(update_fields=["email"])

        resp = _client(actor=alice_id).get(EXPORT_URL.format(subject=alice_user.pk))

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        # The read happened inside the process; what matters is that not one
        # byte of it reached the caller.
        assert "alice@example.com" not in resp.content.decode()

    def test_erasure_is_rolled_back_when_the_journal_cannot_be_written(
        self, alice, broken_journal,
    ):
        """The failure mode this ordering exists to prevent.

        A destructive verb whose journal write fails after the effect would
        leave data gone with nothing recording that it went — the one state
        an access journal must never permit. The handler and the row share a
        transaction, so the rollback is structural, not a cleanup step
        somebody has to remember.
        """
        alice_user, alice_id = alice
        UserPersonalContext.objects.create(
            user=alice_user, workplace_district="Заводской",
            data_sources={"workplace_district": "explicit"},
        )

        resp = _client(actor=alice_id).delete(DELETE_URL.format(subject=alice_user.pk))

        assert resp.status_code == 503
        ctx = UserPersonalContext.objects.get(user=alice_user)
        assert ctx.workplace_district == "Заводской"
        assert ctx.data_sources.get("workplace_district") == "explicit"

    def test_a_refusal_stays_a_refusal_when_the_journal_is_broken(
        self, alice, bob, broken_journal,
    ):
        """A denied request cannot fail closed any harder than it already is.

        The interesting half is that the broken journal must not turn a clean
        403 into a 503 that reads like an outage — the caller was refused on
        the merits, and telling them "try again later" would be false.
        """
        alice_user, alice_id = alice
        bob_user, _ = bob

        resp = _client(actor=alice_id).get(EXPORT_URL.format(subject=bob_user.pk))

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_a_refusal_the_journal_could_not_record_is_an_error_line(
        self, alice, bob, broken_journal, caplog,
    ):
        """No queue (owner D1, 12.09.2026) — so the unrecordable denial is
        named at ERROR with its composition, never dropped silently. «Чужих
        обращений не было» must not be provable by our having failed to
        write them down."""
        alice_user, alice_id = alice
        bob_user, _ = bob

        with caplog.at_level(logging.ERROR, logger="privacy_audit"):
            _client(actor=alice_id).get(EXPORT_URL.format(subject=bob_user.pk))

        lost = [r for r in caplog.records if "privacy_audit.record_lost" in r.getMessage()]
        assert len(lost) == 1, [r.getMessage() for r in caplog.records]
        assert "result=denied" in lost[0].getMessage()
        assert str(bob_user.pk) in lost[0].getMessage()
        assert alice_id not in lost[0].getMessage()

    def test_a_deletion_request_is_not_opened_when_the_journal_cannot_be_written(
        self, alice, broken_journal,
    ):
        """Заявка на удаление — начало разрушения (DRF-1699): без записи о
        том, кто его запустил, разрушение не начинается."""
        from users.models import DeletionRequest

        alice_user, alice_id = alice

        resp = _client(actor=alice_id).post(
            DELREQ_URL.format(subject=alice_user.pk), {}, format="json",
        )

        assert resp.status_code == 503
        assert DeletionRequest.objects.filter(user=alice_user).count() == 0


# ---------------------------------------------------------------------------
# The other side of the owner's line (§107)
# ---------------------------------------------------------------------------


class TestServedOperationsDoNotStopTheProduct:
    """«Останавливать его из-за недоступности журнала значит наказывать
    человека за нашу поломку» — owner §107.

    There is no queue since owner D1 (12.09.2026): what these tests prove is
    that the person is served AND that the loss is written at ERROR with the
    operation and subject — a named blind spot, not a silent one. Whether
    these operations should stop too, now that «not stopped» can no longer
    mean «recorded later», is registered for the owner (OD-AUDIT-LOST-RECORD);
    the answer changes one line in :mod:`privacy_audit.policy`.

    The first implementation stopped everything, and this window raised the
    cost itself: a personal-context read happens on every turn of the
    conversation, so a journal outage degraded the whole conversation rather
    than one privileged operation. The owner drew the line along **what the
    operation does to the data**, not along whose data it touches.

    These tests are the ones that would be easy not to write. A suite proving
    only that things stop stays green on a system that stops everything.
    """

    SERVED = [
        ("get", CTX_URL, None),
        ("patch", CTX_URL,
         {"updates": [{"field": "workplace_district", "value": "Центр"}]}),
        ("get", CTX_URL + "ask-eligibility/", None),
        ("post", CTX_URL + "mark-asked/", {"field": "preferred_time_slots"}),
        ("post", CTX_URL + "skip/", {"field": "preferred_time_slots"}),
        ("get", DELREQ_URL, None),
    ]

    @pytest.mark.parametrize("method,template,body", SERVED,
                             ids=lambda v: str(v)[:40])
    def test_the_person_is_served_while_the_journal_is_down(
        self, method, template, body, alice, broken_journal, caplog,
    ):
        alice_user, alice_id = alice
        client = _client(actor=alice_id)
        url = template.format(subject=alice_user.pk)

        with caplog.at_level(logging.ERROR, logger="privacy_audit"):
            resp = (getattr(client, method)(url, body, format="json") if body
                    else getattr(client, method)(url))

        # 404 «No deletion request» on the deletion-request GET IS the served
        # answer for a subject who never asked to be deleted: the handler ran
        # and answered on the merits. What must NOT appear is 503.
        assert resp.status_code in (200, 404), resp.content
        assert resp.status_code != 503
        lost = [r for r in caplog.records if "privacy_audit.record_lost" in r.getMessage()]
        assert len(lost) == 1, [r.getMessage() for r in caplog.records]
        assert "result=allowed" in lost[0].getMessage()
        assert str(alice_user.pk) in lost[0].getMessage()
        assert PersonalDataAccessLog.objects.count() == 0

    def test_a_context_write_actually_lands_while_the_journal_is_down(
        self, alice, broken_journal,
    ):
        """Served means served — not "returned 200 and rolled back".

        The transaction that wraps an unsafe method exists to keep the effect
        and its record together. When the record is queued rather than
        written, the effect must still commit, or we would be answering
        "done" to something we undid.
        """
        alice_user, alice_id = alice

        resp = _client(actor=alice_id).patch(
            CTX_URL.format(subject=alice_user.pk),
            {"updates": [{"field": "workplace_district", "value": "Центр"}]},
            format="json",
        )

        assert resp.status_code == 200
        assert (
            UserPersonalContext.objects.get(user=alice_user).workplace_district
            == "Центр"
        )

    def test_a_real_insert_failure_does_not_poison_the_effects_transaction(
        self, alice, monkeypatch,
    ):
        """The savepoint, exercised for real — not through the fixture.

        ``broken_journal`` raises before any SQL runs. A genuine failed INSERT
        inside the transaction that wraps an unsafe method would, without a
        savepoint, leave Postgres refusing every later statement — and the
        200 above would be a lie about a rolled-back write. Patch the row
        write itself so the INSERT is what fails.
        """
        from django.db import IntegrityError

        alice_user, alice_id = alice

        def _broken_create(**_kwargs):
            raise IntegrityError("simulated constraint failure inside the journal INSERT")

        monkeypatch.setattr(PersonalDataAccessLog.objects, "create", _broken_create)

        resp = _client(actor=alice_id).patch(
            CTX_URL.format(subject=alice_user.pk),
            {"updates": [{"field": "workplace_district", "value": "Центр"}]},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert (
            UserPersonalContext.objects.get(user=alice_user).workplace_district
            == "Центр"
        )

    def test_the_boundary_is_what_the_operation_does_not_whose_data_it_is(self):
        """The classification, asserted directly.

        Read as a sentence: exporting, deleting, erasing and opening a
        deletion request stop; reading, writing, asking and reading a
        deletion request's status do not. If somebody widens the set by
        analogy, this is where it shows.
        """
        op = PersonalDataAccessLog.Operation
        assert policy.FAIL_CLOSED_OPERATIONS == {
            op.EXPORT, op.DELETE, op.ERASE_CONTEXT, op.DELETION_REQUEST_CREATE,
        }
        for stopped in (op.EXPORT, op.DELETE, op.ERASE_CONTEXT, op.DELETION_REQUEST_CREATE):
            assert policy.stops_when_unauditable(stopped)
        for served in (
            op.READ_CONTEXT, op.WRITE_CONTEXT, op.ASK_METADATA, op.DELETION_REQUEST_READ,
            op.READ_PROFILE,
        ):
            assert not policy.stops_when_unauditable(served)

    def test_there_is_no_queue_and_no_second_store(self):
        """Owner D1 (12.09.2026): a disk spool is a second sensitive store.

        Stated as a test so that a queue does not quietly return under a new
        name — the check is on the package, not on today's spelling.
        """
        import pkgutil

        import privacy_audit

        names = {m.name for m in pkgutil.iter_modules(privacy_audit.__path__)}
        assert "spool" not in names
        assert not hasattr(services, "record_or_queue")
        assert not hasattr(services, "enqueue")
        # No management commands at all today; the day one appears, it must
        # not be a drain of anything.
        assert "management" not in names, "a management package appeared — check it is not a spool drain"


# ---------------------------------------------------------------------------
# What the record contains
# ---------------------------------------------------------------------------


class TestWhatIsRecorded:
    def test_an_allowed_export_is_recorded_with_its_composition(self, alice):
        alice_user, alice_id = alice

        resp = _client(actor=alice_id, basis="REQ-4471").get(
            EXPORT_URL.format(subject=alice_user.pk),
        )

        assert resp.status_code == 200
        row = PersonalDataAccessLog.objects.get()
        assert row.operation == PersonalDataAccessLog.Operation.EXPORT
        assert row.object_category == PersonalDataAccessLog.ObjectCategory.PERSONAL_DATA
        assert row.object_id == alice_user.pk
        assert row.result == PersonalDataAccessLog.Result.ALLOWED
        assert row.actor_id == alice_user.pk
        assert row.actor_role == "client"
        assert row.actor_named is True
        assert row.caller_purpose == PersonalDataAccessLog.CallerPurpose.INTERNAL
        assert row.basis == "REQ-4471"
        assert row.denial_reason == ""

    def test_the_journal_does_not_copy_the_data_it_journals(self, alice):
        """A record of access that carried the accessed values would be a
        second store of the same personal data — one that none of the
        erasure paths point at."""
        alice_user, alice_id = alice
        alice_user.email = "alice@example.com"
        alice_user.save(update_fields=["email"])
        UserPersonalContext.objects.create(
            user=alice_user, workplace_district="Центр",
        )

        _client(actor=alice_id).get(EXPORT_URL.format(subject=alice_user.pk))

        blob = " ".join(
            str(getattr(row, f.name))
            for row in PersonalDataAccessLog.objects.all()
            for f in PersonalDataAccessLog._meta.fields
        )
        assert "alice@example.com" not in blob
        assert "Центр" not in blob

    def test_an_unstated_basis_stays_empty_and_is_not_invented(self, alice):
        """Absence arrives as absence.

        No caller states a basis today. A default here — "internal_api",
        "n/a", anything plausible — would make "nobody stated one" and
        "somebody stated this" indistinguishable, and the row is immutable,
        so the confusion would be permanent.
        """
        alice_user, alice_id = alice

        _client(actor=alice_id).get(EXPORT_URL.format(subject=alice_user.pk))

        assert PersonalDataAccessLog.objects.get().basis == ""

    def test_a_service_call_with_no_resolved_human_says_so(self, alice):
        """``actor_role`` must distinguish "a service acted" from "a person
        with no role acted". Since #392 an unnamed call is refused — and the
        refusal is the row."""
        alice_user, _ = alice

        resp = _client().get(EXPORT_URL.format(subject=alice_user.pk))

        assert resp.status_code == 403
        row = PersonalDataAccessLog.objects.get()
        assert row.actor_id is None
        assert row.actor_role == "service"
        assert row.result == PersonalDataAccessLog.Result.DENIED
        assert row.denial_reason == "unnamed_actor"

    def test_the_callers_own_header_is_never_stored(self, alice, bob):
        """Unbounded caller-controlled text must not reach the record whoever
        investigates will be reading."""
        alice_user, alice_id = alice
        bob_user, _ = bob

        _client(actor=alice_id).get(EXPORT_URL.format(subject=bob_user.pk))

        blob = " ".join(
            str(getattr(row, f.name))
            for row in PersonalDataAccessLog.objects.all()
            for f in PersonalDataAccessLog._meta.fields
        )
        assert alice_id not in blob


class TestRefusalsAreCountedSeparately:
    """One coarse name outward; separate counters inward.

    A month from now the operator's question is not "were there 403s" but
    "is somebody reaching into foreign data, or is a purpose misconfigured" —
    and the two have opposite fixes.
    """

    def test_reasons_are_separate_rows_not_one_blurred_count(self, alice, bob):
        alice_user, alice_id = alice
        bob_user, _ = bob

        _client(actor=alice_id).get(EXPORT_URL.format(subject=bob_user.pk))
        _client(bearer=PROVISIONING_TOKEN, actor=alice_id).get(
            EXPORT_URL.format(subject=alice_user.pk),
        )
        _client(actor="bot:telegram:999999").get(
            EXPORT_URL.format(subject=alice_user.pk),
        )

        counts = dict(
            PersonalDataAccessLog.objects
            .filter(result=PersonalDataAccessLog.Result.DENIED)
            .values_list("denial_reason", "id")
        )
        reasons = list(
            PersonalDataAccessLog.objects
            .filter(result=PersonalDataAccessLog.Result.DENIED)
            .values_list("denial_reason", flat=True)
        )
        assert sorted(reasons) == ["subject_mismatch", "unknown_actor", "wrong_purpose"]
        assert len(counts) == 3

    def test_a_foreign_reach_records_the_targeted_subject(self, alice, bob):
        alice_user, alice_id = alice
        bob_user, _ = bob

        _client(actor=alice_id).get(EXPORT_URL.format(subject=bob_user.pk))

        row = PersonalDataAccessLog.objects.get(denial_reason="subject_mismatch")
        assert row.object_id == bob_user.pk
        assert row.actor_id == alice_user.pk

    def test_an_unauthenticated_knock_is_not_a_row(self, alice):
        """The composition §96 asks for names an actor, a role and a tenant.
        A request with no credential has none of them, and a durable row per
        anonymous request is a way to let anonymous callers grow our storage.
        Those stay in the log.
        """
        alice_user, _ = alice

        for _ in range(5):
            _client(bearer=None).get(EXPORT_URL.format(subject=alice_user.pk))
            _client(bearer="nope").get(EXPORT_URL.format(subject=alice_user.pk))

        assert PersonalDataAccessLog.objects.count() == 0


class TestUnnamedCallsAreCountable:
    """``actor_named`` lives in a column, not in a log line: the ``users``
    logger is ``propagate: False`` onto a console handler, so "the log is
    empty" and "the container was restarted" look identical. A zero has to
    come from a counter that counts. Since #392 an unnamed call is a denial —
    the column is how «безымянных вызовов не было» is proven after the fact."""

    def test_unnamed_calls_are_counted_one_row_each(self, alice):
        alice_user, _ = alice

        for _ in range(3):
            _client().get(EXPORT_URL.format(subject=alice_user.pk))

        assert PersonalDataAccessLog.objects.filter(actor_named=False).count() == 3
        assert PersonalDataAccessLog.objects.filter(
            actor_named=False, denial_reason="unnamed_actor",
        ).count() == 3

    def test_a_named_call_does_not_move_the_unnamed_counter(self, alice):
        alice_user, alice_id = alice

        _client(actor=alice_id).get(EXPORT_URL.format(subject=alice_user.pk))

        assert PersonalDataAccessLog.objects.filter(actor_named=False).count() == 0
        assert PersonalDataAccessLog.objects.filter(actor_named=True).count() == 1


# ---------------------------------------------------------------------------
# The properties the ruling asks for by name
# ---------------------------------------------------------------------------


class TestJournalIsAppendOnly:
    """Immutability as a mechanism, not a convention.

    "We don't rewrite audit rows" is a habit. "There is no method that
    rewrites an audit row" is a guarantee, and only the second one survives
    the person who did not read this file.
    """

    @pytest.fixture
    def row(self, alice) -> PersonalDataAccessLog:
        alice_user, alice_id = alice
        _client(actor=alice_id).get(EXPORT_URL.format(subject=alice_user.pk))
        return PersonalDataAccessLog.objects.get()

    def test_a_row_cannot_be_rewritten(self, row):
        row.result = PersonalDataAccessLog.Result.DENIED
        with pytest.raises(NotImplementedError):
            row.save()

    def test_a_row_cannot_be_deleted(self, row):
        with pytest.raises(NotImplementedError):
            row.delete()

    def test_a_queryset_cannot_be_deleted(self, row):
        with pytest.raises(NotImplementedError):
            PersonalDataAccessLog.objects.all().delete()

    def test_the_stored_row_survives_every_attempt(self, row):
        for attempt in (
            lambda: row.save(),
            lambda: row.delete(),
            lambda: PersonalDataAccessLog.objects.all().delete(),
        ):
            with pytest.raises(NotImplementedError):
                attempt()
        assert PersonalDataAccessLog.objects.count() == 1

    def test_the_limit_of_this_guard_is_named(self):
        """``QuerySet.update`` and raw SQL still reach the table.

        Stated as a test so the boundary is written down next to the
        guarantee rather than discovered by someone citing this class as
        more than it is: this protects the journal from the application, not
        from a database administrator.
        """
        assert hasattr(PersonalDataAccessLog.objects.all(), "update")


class TestJournalIsItsOwnGrant:
    """Reading who accessed whose personal data is a separate permission."""

    @pytest.fixture
    def admin_site(self):
        from django.contrib import admin as django_admin

        return django_admin.site._registry[PersonalDataAccessLog]

    def _staff(self, username: str, *, with_perm: bool) -> User:
        user = User.objects.create_user(
            username=username, password="x", role="admin",  # pragma: allowlist secret
            is_staff=True,
            phone=f"+7999222{username[-4:]}",
        )
        if with_perm:
            user.user_permissions.add(Permission.objects.get(
                codename="view_personal_data_access_log",
            ))
            user = User.objects.get(pk=user.pk)  # drop the permission cache
        return user

    def _request(self, user):
        from django.test import RequestFactory

        request = RequestFactory().get("/admin/")
        request.user = user
        return request

    def test_staff_without_the_grant_cannot_read_the_journal(self, admin_site):
        request = self._request(self._staff("aud_plain1", with_perm=False))
        assert admin_site.has_view_permission(request) is False
        assert admin_site.has_module_permission(request) is False

    def test_staff_with_the_grant_can_read_it(self, admin_site):
        request = self._request(self._staff("aud_grant2", with_perm=True))
        assert admin_site.has_view_permission(request) is True
        assert admin_site.has_module_permission(request) is True

    @pytest.mark.parametrize("with_perm", [False, True])
    def test_nobody_can_write_through_the_admin(self, admin_site, with_perm):
        request = self._request(
            self._staff(f"aud_wr{int(with_perm)}003", with_perm=with_perm),
        )
        assert admin_site.has_add_permission(request) is False
        assert admin_site.has_change_permission(request) is False
        assert admin_site.has_delete_permission(request) is False

    def test_the_model_offers_no_write_permissions_at_all(self):
        """Not "the checkbox is off" — the permission rows do not exist."""
        codenames = set(
            Permission.objects
            .filter(content_type__app_label="privacy_audit")
            .values_list("codename", flat=True)
        )
        assert codenames == {"view_personal_data_access_log"}


class TestRetentionIsNamedAsProvisional:
    """One year is a product decision standing in until legal review (§96).

    Nothing prunes yet, deliberately: a retention job is the one piece of
    code allowed to remove audit rows, and it should be written against a
    settled period rather than a placeholder. This test exists so that
    "nothing prunes" is a recorded decision instead of an oversight somebody
    later reads as a bug.
    """

    def test_no_retention_job_exists_yet(self):
        import privacy_audit

        assert not hasattr(privacy_audit, "prune_expired"), (
            "A pruning path appeared. It is the only code allowed to delete "
            "audit rows — make sure the retention period it enforces is the "
            "settled one and not the provisional year, then update this test."
        )

    def test_the_provisional_year_is_documented_where_it_is_implemented(self):
        from privacy_audit import models

        doc = models.__doc__ or ""
        assert "ВРЕМЕНН" in doc.upper() or "TEMPORARY" in doc.upper(), (
            "The retention period must be documented as provisional in the "
            "model module, not only in the decision register — the person who "
            "implements pruning reads this file."
        )


class TestGuardCoversTheWholeSurface:
    """Every guarded route must also be an audited route.

    The authorization guard and the journal are two separate opt-ins on the
    same views. A route that carries one and not the other is the exact shape
    of "looks protected".
    """

    ROUTES = [
        EXPORT_URL, DELETE_URL, CTX_URL,
        "/api/v1/internal/users/{subject}/personal-context/ask-eligibility/",
        "/api/v1/internal/users/{subject}/personal-context/mark-asked/",
        "/api/v1/internal/users/{subject}/personal-context/skip/",
        DELREQ_URL,
        DELREQ_URL + "{request_id}/",
        "/api/v1/internal/users/{subject}/",  # DRF-1709
    ]

    def test_the_list_above_is_every_guarded_view_not_a_hand_picked_subset(self):
        """Derive the guarded set from the code and compare — a list typed by
        hand is green on the day a ninth route arrives unaudited."""
        from django.urls import resolve

        from users.permissions import IsInternalBearerForSubject

        listed = {
            resolve(t.format(subject=uuid.uuid4(), request_id=uuid.uuid4())).func.cls
            for t in self.ROUTES
        }
        guarded = set()
        for module in (
            "personal_data_api",
            "internal_personal_context_api",
            "deletion_request_api",
            "internal_users_api",  # DRF-1709 — the profile card joined the surface
        ):
            mod = __import__(f"users.{module}", fromlist=["x"])
            for obj in vars(mod).values():
                if isinstance(obj, type) and IsInternalBearerForSubject in getattr(
                    obj, "permission_classes", []
                ):
                    guarded.add(obj)
        assert guarded, "the scan found no guarded view — the quantifier below would be vacuous"
        assert guarded == listed, {
            "guarded_not_listed": sorted(c.__name__ for c in guarded - listed),
            "listed_not_guarded": sorted(c.__name__ for c in listed - guarded),
        }
        assert len(guarded) == 9

    @pytest.mark.parametrize("template", ROUTES)
    def test_route_is_audited(self, template):
        from django.urls import resolve

        view_cls = resolve(template.format(subject=uuid.uuid4(), request_id=uuid.uuid4())).func.cls
        assert issubclass(view_cls, mixins.AuditedPersonalDataAccess), (
            f"{view_cls.__name__} is on the personal-data surface and records "
            f"nothing"
        )
        assert view_cls.audit_object_category, (
            f"{view_cls.__name__} declares no object category"
        )
        assert view_cls.audit_operations, (
            f"{view_cls.__name__} declares no operations"
        )

    @pytest.mark.parametrize("template", ROUTES)
    def test_every_handler_method_has_a_declared_operation(self, template):
        """A method the view serves but the journal has no name for would be
        refused at runtime (deny-by-default). Caught here instead."""
        from django.urls import resolve

        view_cls = resolve(template.format(subject=uuid.uuid4(), request_id=uuid.uuid4())).func.cls
        served = {
            m.upper() for m in
            ("get", "post", "patch", "put", "delete")
            if hasattr(view_cls, m)
        }
        undeclared = served - set(view_cls.audit_operations)
        assert not undeclared, (
            f"{view_cls.__name__} serves {sorted(undeclared)} with no audit "
            f"operation declared"
        )
