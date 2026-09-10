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

from privacy_audit import mixins, policy, services, spool
from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.services import AuditUnavailable
from users.models import User, UserPersonalContext

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token"  # noqa: S105
PROVISIONING_TOKEN = "test-provisioning-only-token"  # noqa: S105

EXPORT_URL = "/api/v1/internal/users/{subject}/personal-data/export/"
DELETE_URL = "/api/v1/internal/users/{subject}/personal-data/"
CTX_URL = "/api/v1/internal/users/{subject}/personal-context/"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING_TOKEN
    settings.INTERNAL_SUBJECT_AUTHZ_ENFORCE = False
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
        username=nick, password="x", role="client", phone=f"+7999111{nick[-4:]}",
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


@pytest.fixture(autouse=True)
def isolated_spool(settings, tmp_path):
    """Each test gets its own queue directory.

    Not a convenience: a shared spool would let one test's queued record be
    drained by another and counted as that one's evidence.
    """
    settings.PRIVACY_AUDIT_SPOOL_DIR = str(tmp_path / "spool")
    return settings.PRIVACY_AUDIT_SPOOL_DIR


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
        assert resp.json()["error"]["code"] == "CLIENT_MISMATCH"
        # And the attempt is not lost: it goes to the queue, because "no
        # foreign access happened" must not be provable merely by our having
        # failed to write it down.
        assert spool.pending_count() == 1


# ---------------------------------------------------------------------------
# The other side of the owner's line (§107)
# ---------------------------------------------------------------------------


class TestQueuedOperationsDoNotStopTheProduct:
    """«Останавливать его из-за недоступности журнала значит наказывать
    человека за нашу поломку» — owner §107.

    The first implementation stopped everything, and this window raised the
    cost itself: a personal-context read happens on every turn of the
    conversation, so a journal outage degraded the whole conversation rather
    than one privileged operation. The owner drew the line along **what the
    operation does to the data**, not along whose data it touches.

    These tests are the ones that would be easy not to write. A suite proving
    only that things stop stays green on a system that stops everything.
    """

    QUEUED = [
        ("get", CTX_URL, None),
        ("patch", CTX_URL,
         {"updates": [{"field": "workplace_district", "value": "Центр"}]}),
        ("get", CTX_URL + "ask-eligibility/", None),
        ("post", CTX_URL + "mark-asked/", {"field": "preferred_time_slots"}),
        ("post", CTX_URL + "skip/", {"field": "preferred_time_slots"}),
    ]

    @pytest.mark.parametrize("method,template,body", QUEUED,
                             ids=lambda v: str(v)[:40])
    def test_the_person_is_served_while_the_journal_is_down(
        self, method, template, body, alice, broken_journal,
    ):
        alice_user, alice_id = alice
        client = _client(actor=alice_id)
        url = template.format(subject=alice_user.pk)

        resp = (getattr(client, method)(url, body, format="json") if body
                else getattr(client, method)(url))

        assert resp.status_code == 200, resp.content
        assert spool.pending_count() == 1

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
        assert spool.pending_count() == 1

    def test_the_boundary_is_what_the_operation_does_not_whose_data_it_is(self):
        """The classification, asserted directly.

        Read as a sentence: exporting, deleting and erasing stop; reading,
        writing and asking do not. If somebody widens the set by analogy, this
        is where it shows.
        """
        _Op = PersonalDataAccessLog.Operation
        assert policy.FAIL_CLOSED_OPERATIONS == {
            _Op.EXPORT, _Op.DELETE, _Op.ERASE_CONTEXT,
        }
        for stopped in (_Op.EXPORT, _Op.DELETE, _Op.ERASE_CONTEXT):
            assert policy.stops_when_unauditable(stopped)
        for served in (_Op.READ_CONTEXT, _Op.WRITE_CONTEXT, _Op.ASK_METADATA):
            assert not policy.stops_when_unauditable(served)


class TestTheQueueIsAnObligation:
    """«Очередь без доказанной доставки — то же самое, что отсутствие записи,
    только выглядит спокойнее.»

    So the queue is tested by what comes OUT of it, not by what goes in.
    """

    def test_queued_records_reach_the_journal_and_the_counter_returns_to_zero(
        self, alice, broken_journal,
    ):
        """The positive guard on the counter.

        Three queued accesses must give three — a counter that always said
        zero would pass a test that only checked the end state, and "nothing
        is waiting" would then be indistinguishable from "nothing is counted".
        """
        alice_user, alice_id = alice
        for _ in range(3):
            _client(actor=alice_id).get(CTX_URL.format(subject=alice_user.pk))

        assert spool.pending_count() == 3
        assert PersonalDataAccessLog.objects.count() == 0

        written, failed = spool.drain()

        assert (written, failed) == (3, 0)
        assert spool.pending_count() == 0
        assert PersonalDataAccessLog.objects.count() == 3

    def test_a_replayed_record_is_the_same_record(self, alice, broken_journal):
        """The queue must not store a different shape than the table.

        A queue that replays into a journal disagreeing with itself is worse
        than no queue: nobody notices until somebody reads a year-old row.
        """
        alice_user, alice_id = alice
        _client(actor=alice_id, basis="REQ-9001").get(
            CTX_URL.format(subject=alice_user.pk),
        )
        spool.drain()

        row = PersonalDataAccessLog.objects.get()
        assert row.operation == PersonalDataAccessLog.Operation.READ_CONTEXT
        assert row.object_id == alice_user.pk
        assert row.actor_id == alice_user.pk
        assert row.actor_role == "client"
        assert row.actor_named is True
        assert row.result == PersonalDataAccessLog.Result.ALLOWED
        assert row.basis == "REQ-9001"

    def test_a_queued_denial_reaches_the_journal_with_its_reason(
        self, alice, bob, broken_journal,
    ):
        alice_user, alice_id = alice
        bob_user, _ = bob

        _client(actor=alice_id).get(CTX_URL.format(subject=bob_user.pk))
        spool.drain()

        row = PersonalDataAccessLog.objects.get()
        assert row.result == PersonalDataAccessLog.Result.DENIED
        assert row.denial_reason == "subject_mismatch"
        assert row.object_id == bob_user.pk

    def test_the_queue_carries_no_personal_values(self, alice, broken_journal):
        """A spool file is a file on a disk somebody can read."""
        alice_user, alice_id = alice
        alice_user.email = "alice@example.com"
        alice_user.save(update_fields=["email"])
        UserPersonalContext.objects.create(
            user=alice_user, workplace_district="Центр",
        )

        _client(actor=alice_id).get(CTX_URL.format(subject=alice_user.pk))

        blob = "".join(
            path.read_text(encoding="utf-8")
            for path in spool.spool_dir().glob("*.audit.json")
        )
        assert blob
        assert "alice@example.com" not in blob
        assert "Центр" not in blob
        assert alice_id not in blob

    def test_a_partial_write_never_becomes_a_record(self, alice, broken_journal):
        """Records appear whole or not at all.

        Written to a temporary name and renamed, so a process that dies
        mid-write leaves nothing the drain would read as truth.
        """
        alice_user, alice_id = alice
        _client(actor=alice_id).get(CTX_URL.format(subject=alice_user.pk))

        leftovers = list(spool.spool_dir().glob("*.partial"))
        assert leftovers == []

    def test_drain_leaves_what_it_could_not_write(self, alice, broken_journal,
                                                  monkeypatch):
        """A record leaves the queue only once its row exists.

        Otherwise a drain that half-succeeded would report success and take
        the evidence with it.
        """
        alice_user, alice_id = alice
        _client(actor=alice_id).get(CTX_URL.format(subject=alice_user.pk))
        assert spool.pending_count() == 1

        def _refuse(*_args, **_kwargs):
            raise RuntimeError("journal still down")

        monkeypatch.setattr(
            PersonalDataAccessLog.objects, "create", _refuse, raising=False,
        )
        written, failed = spool.drain()

        assert (written, failed) == (0, 1)
        assert spool.pending_count() == 1

    def test_the_only_loss_path_is_named_and_does_not_stop_the_person(
        self, alice, broken_journal, monkeypatch,
    ):
        """Both the table and the queue gone.

        The record is lost — that is the single path on which it happens, and
        for a queued operation the owner's ruling says the product does not
        stop. Asserted rather than left implicit, so the loss is a known limit
        and not a discovery.
        """
        alice_user, alice_id = alice

        def _boom(_record):
            raise spool.SpoolUnavailable("disk gone")

        # ``services`` imports the module lazily, so the name is looked up on
        # the module object at call time — patching it here is what the code
        # under test will actually see.
        monkeypatch.setattr(spool, "enqueue", _boom)

        resp = _client(actor=alice_id).get(CTX_URL.format(subject=alice_user.pk))

        assert resp.status_code == 200
        assert PersonalDataAccessLog.objects.count() == 0
        assert spool.pending_count() == 0


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
        with no role acted"."""
        alice_user, _ = alice

        _client().get(EXPORT_URL.format(subject=alice_user.pk))

        row = PersonalDataAccessLog.objects.get()
        assert row.actor_id is None
        assert row.actor_role == "service"

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


class TestStagedCounterIsQueryable:
    """The number that earns the flag flip.

    It lives in a column, not in a log line, for a concrete reason: the
    ``users`` logger this surface writes to is configured ``propagate: False``
    onto a console handler, so "the log is empty" and "the container was
    restarted" look identical. A zero has to come from a counter that counts.
    """

    def test_unnamed_calls_are_counted_one_row_each(self, alice):
        alice_user, _ = alice

        for _ in range(3):
            _client().get(EXPORT_URL.format(subject=alice_user.pk))

        assert PersonalDataAccessLog.objects.filter(actor_named=False).count() == 3

    def test_a_named_call_does_not_move_the_unnamed_counter(self, alice):
        alice_user, alice_id = alice

        _client(actor=alice_id).get(EXPORT_URL.format(subject=alice_user.pk))

        assert PersonalDataAccessLog.objects.filter(actor_named=False).count() == 0
        assert PersonalDataAccessLog.objects.filter(actor_named=True).count() == 1

    def test_with_the_flag_on_the_unnamed_call_is_a_denial_row(self, settings, alice):
        settings.INTERNAL_SUBJECT_AUTHZ_ENFORCE = True
        alice_user, _ = alice

        resp = _client().get(EXPORT_URL.format(subject=alice_user.pk))

        assert resp.status_code in (401, 403)
        row = PersonalDataAccessLog.objects.get()
        assert row.result == PersonalDataAccessLog.Result.DENIED
        assert row.denial_reason == "unnamed_actor"


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
            username=username, password="x", role="admin", is_staff=True,
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
    ]

    @pytest.mark.parametrize("template", ROUTES)
    def test_route_is_audited(self, template):
        from django.urls import resolve

        view_cls = resolve(template.format(subject=uuid.uuid4())).func.cls
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

        view_cls = resolve(template.format(subject=uuid.uuid4())).func.cls
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
