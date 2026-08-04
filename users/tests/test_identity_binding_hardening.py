"""Security-hardening tests for the identity binding (E2E-BOT-02B).

Covers the four review findings closed on top of the base patch:

* P1-1 race safety — REAL concurrent binds on PostgreSQL (threads +
  ``transaction=True``, no mocks): same-target race and
  different-target race.
* P1-2 trust boundary — contract narrowing: staff/admin/proxy targets
  are rejected with the same info-hidden error as unknown ids.
* P1-3 durable audit — every outcome (created / idempotent /
  conflict / rejected) lands an ``external_identity_bound``
  AnalyticsEvent row; no secrets in the payload.
* Model invariants — DB-level CheckConstraints (proxy-only pointer,
  no self-link) and fail-closed behaviour on physical target delete.
"""
from __future__ import annotations

import threading
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from users.models import User
from users.services import (
    BindTargetNotFoundError,
    ExternalIdentityNotFoundError,
    IdentityBindingConflictError,
    InvalidExternalUserIDError,
    bind_external_identity,
    resolve_external_user,
    unlink_external_identity,
)


def _real_user(username, phone, role="client", **extra):
    return User.objects.create_user(
        username=username, password="x", role=role, phone=phone,
        is_proxy=False, **extra,
    )


def _audit_events(external_user_id):
    return list(
        AnalyticsEvent.objects.filter(
            event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
            payload__external_user_id=external_user_id,
        ).order_by("created_at")
    )


# ── P1-1: real concurrency on PostgreSQL ─────────────────────────


def _run_concurrent_binds(external_user_id, targets):
    """Fire one thread per target at the same external identity.

    Each thread runs its own DB connection (Django connections are
    thread-local) and commits for real — this exercises the actual
    row-lock path, not a serialized mock.
    """
    barrier = threading.Barrier(len(targets))
    outcomes = [None] * len(targets)

    def worker(index, target_pk):
        try:
            barrier.wait(timeout=10)
            bind_external_identity(external_user_id, target_pk)
            outcomes[index] = "ok"
        except IdentityBindingConflictError:
            outcomes[index] = "conflict"
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(i, t.pk))
        for i, t in enumerate(targets)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(o is not None for o in outcomes), "worker thread hung"
    return outcomes


@pytest.mark.django_db(transaction=True)
class TestConcurrentBinding:
    def test_same_target_race_both_safe_single_binding(self):
        real = _real_user("race-same", "+79991110001")
        outcomes = _run_concurrent_binds("bot:max:race-same", [real, real])
        assert outcomes == ["ok", "ok"]
        proxy = User.objects.get(username="bot:max:race-same")
        assert proxy.linked_user_id == real.pk
        assert resolve_external_user("bot:max:race-same").pk == real.pk

    def test_different_target_race_exactly_one_wins(self):
        first = _real_user("race-a", "+79991110002")
        second = _real_user("race-b", "+79991110003")
        outcomes = _run_concurrent_binds(
            "bot:max:race-diff", [first, second],
        )
        assert sorted(outcomes) == ["conflict", "ok"]
        # The committed binding is authoritative and singular —
        # whichever thread won, the loser did NOT overwrite it.
        proxy = User.objects.get(username="bot:max:race-diff")
        winner = first if outcomes[0] == "ok" else second
        assert proxy.linked_user_id == winner.pk
        assert resolve_external_user("bot:max:race-diff").pk == winner.pk

    def test_race_loser_does_not_clobber_existing_binding(self):
        bound = _real_user("race-bound", "+79991110004")
        challenger = _real_user("race-challenger", "+79991110005")
        bind_external_identity("bot:max:race-clobber", bound.pk)
        outcomes = _run_concurrent_binds(
            "bot:max:race-clobber", [bound, challenger],
        )
        # One thread re-binds the same pair (idempotent ok), the other
        # conflicts — the original binding is never moved.
        assert sorted(outcomes) == ["conflict", "ok"]
        assert resolve_external_user("bot:max:race-clobber").pk == bound.pk

    def test_target_soft_delete_race_stays_consistent(self):
        """The target is re-validated UNDER LOCK inside the binding
        transaction: a concurrent soft-delete lands either before the
        locked re-read (bind → controlled 404, no binding) or after the
        commit (binding exists, resolver fail-closed). Never a crash,
        never a binding committed ON TOP of an already-deleted account
        in the same instant."""
        real = _real_user("race-del", "+79991110006")
        barrier = threading.Barrier(2)
        outcome = []

        def binder():
            try:
                barrier.wait(timeout=10)
                bind_external_identity("bot:max:race-del", real.pk)
                outcome.append("ok")
            except BindTargetNotFoundError:
                outcome.append("not_found")
            finally:
                connection.close()

        def deleter():
            try:
                barrier.wait(timeout=10)
                User.objects.filter(pk=real.pk).update(
                    is_active=False, deleted_at=timezone.now(),
                )
            finally:
                connection.close()

        threads = [threading.Thread(target=binder),
                   threading.Thread(target=deleter)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(outcome) == 1, "binder thread hung"
        proxy = User.objects.filter(username="bot:max:race-del").first()
        real.refresh_from_db()
        if outcome == ["not_found"]:
            # Pre-check rejection creates no proxy at all; in-tx
            # rejection leaves it unbound. Either way: no binding.
            assert proxy is None or proxy.linked_user_id is None
        else:
            # Bind won the lock before the delete: binding exists, and
            # resolution is fail-closed for the now-deleted account.
            assert proxy.linked_user_id == real.pk
            assert resolve_external_user("bot:max:race-del").is_proxy is True


# ── P1-2: trust boundary — target contract narrowing ─────────────


@pytest.mark.django_db
class TestBindTargetTrustBoundary:
    def test_specialist_target_rejected(self):
        specialist = _real_user("bind-spec", "+79992220001", role="specialist")
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:to-specialist", specialist.pk)

    def test_admin_target_rejected(self):
        admin = _real_user("bind-admin", "+79992220002", role="admin")
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:to-admin", admin.pk)

    def test_inactive_target_rejected(self):
        real = _real_user("bind-inactive", "+79992220003", is_active=False)
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:to-inactive", real.pk)

    def test_arbitrary_rebind_impossible_after_conflict(self):
        """A caller holding the bearer cannot move a binding once set:
        every subsequent bind attempt to another account conflicts."""
        bound = _real_user("bind-fixed", "+79992220004")
        bind_external_identity("bot:max:fixed", bound.pk)
        for i in range(3):
            other = _real_user(f"bind-other-{i}", f"+7999222001{i}")
            with pytest.raises(IdentityBindingConflictError):
                bind_external_identity("bot:max:fixed", other.pk)
        assert resolve_external_user("bot:max:fixed").pk == bound.pk

    def test_malformed_uuid_target_controlled_404(self):
        """Direct service calls (bootstrap, ops scripts) bypass the DRF
        serializer: a non-UUID ayla_user_id must still surface as the
        documented BindTargetNotFoundError, not Django's ValidationError."""
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:bad-uuid", "not-a-uuid")
        (event,) = _audit_events("bot:max:bad-uuid")
        assert event.payload["result"] == "rejected"
        assert event.payload["reason"] == "target_not_bindable"

    def test_tenanted_client_target_binds_person_level(self):
        """Client accounts are multi-provider by design (#246: no
        tenant JWT claim for role=client; DEC-0016: Subject is not
        owned by a tenant). A client row carrying a legacy tenant FK
        is still a valid person-level binding target."""
        from tenants.models import Tenant
        tenant = Tenant.objects.create(slug="bind-tenant", name="Bind Tenant")
        real = _real_user("bind-tenanted", "+79992220005", tenant=tenant)
        proxy, _ = bind_external_identity("bot:max:tenanted", real.pk)
        assert proxy.linked_user_id == real.pk
        assert resolve_external_user("bot:max:tenanted").pk == real.pk


# ── P1-3: durable audit trail ────────────────────────────────────


@pytest.mark.django_db
class TestBindingAuditTrail:
    def test_successful_bind_audited(self):
        real = _real_user("audit-create", "+79993330001")
        proxy, _ = bind_external_identity(
            "bot:max:audit-create", real.pk,
            initiator="internal_api", request_id="req-1",
        )
        (event,) = _audit_events("bot:max:audit-create")
        assert event.actor_id == real.pk
        assert event.payload == {
            "operation": "bind_external_identity",
            "external_user_id": "bot:max:audit-create",
            "proxy_user_id": str(proxy.pk),
            "target_user_id": str(real.pk),
            "result": "created",
            "reason": "bound",
            "initiator": "internal_api",
            "request_id": "req-1",
        }

    def test_idempotent_repeat_audited(self):
        real = _real_user("audit-idem", "+79993330002")
        bind_external_identity("bot:max:audit-idem", real.pk)
        bind_external_identity("bot:max:audit-idem", real.pk)
        results = [e.payload["result"] for e in _audit_events("bot:max:audit-idem")]
        assert results == ["created", "idempotent"]

    def test_identical_retry_deduped_by_deterministic_key(self):
        """The audit client_event_id is deterministic per logical
        operation: a third identical bind hits the (actor,
        client_event_id) dedup constraint instead of writing a second
        'idempotent' row — and the dedup never poisons the caller's
        transaction."""
        real = _real_user("audit-dedup", "+79993330007")
        bind_external_identity("bot:max:audit-dedup", real.pk)
        bind_external_identity("bot:max:audit-dedup", real.pk)
        bind_external_identity("bot:max:audit-dedup", real.pk)
        assert resolve_external_user("bot:max:audit-dedup").pk == real.pk
        results = [
            e.payload["result"] for e in _audit_events("bot:max:audit-dedup")
        ]
        assert results == ["created", "idempotent"]

    def test_conflict_audited_and_binding_untouched(self):
        bound = _real_user("audit-conflict-a", "+79993330003")
        other = _real_user("audit-conflict-b", "+79993330004")
        bind_external_identity("bot:max:audit-conflict", bound.pk)
        with pytest.raises(IdentityBindingConflictError):
            bind_external_identity("bot:max:audit-conflict", other.pk)
        events = _audit_events("bot:max:audit-conflict")
        assert [e.payload["result"] for e in events] == ["created", "conflict"]
        conflict = events[-1]
        assert conflict.payload["reason"] == "already_bound_different_target"
        assert conflict.actor_id == other.pk  # the attempted target
        assert resolve_external_user("bot:max:audit-conflict").pk == bound.pk

    def test_rejected_unknown_target_audited(self):
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:audit-404", uuid4())
        (event,) = _audit_events("bot:max:audit-404")
        assert event.actor_id is None
        assert event.payload["result"] == "rejected"
        assert event.payload["reason"] == "target_not_bindable"

    def test_rejected_invalid_external_id_audited(self):
        with pytest.raises(InvalidExternalUserIDError):
            bind_external_identity("not-an-external-id", uuid4())
        (event,) = _audit_events("not-an-external-id")
        assert event.payload["result"] == "rejected"
        assert event.payload["reason"] == "invalid_external_user_id"

    def test_audit_payload_carries_no_secrets(self):
        real = _real_user("audit-clean", "+79993330005")
        bind_external_identity(
            "bot:max:audit-clean", real.pk, request_id="req-42",
        )
        (event,) = _audit_events("bot:max:audit-clean")
        blob = repr(event.payload).lower()
        for marker in ("token", "bearer", "authorization", "secret"):
            assert marker not in blob
        assert set(event.payload) == {
            "operation", "external_user_id", "proxy_user_id",
            "target_user_id", "result", "reason", "initiator",
            "request_id",
        }

    def test_audit_write_failure_rolls_back_binding(self, monkeypatch):
        """P1-2 atomicity: the success audit is STRICT and lives inside
        the binding transaction — a broken audit sink fails the whole
        operation and rolls the identity mutation back. No committed
        binding without its audit row."""
        real = _real_user("audit-atomic", "+79993330006")

        def boom(*args, **kwargs):
            raise RuntimeError("audit sink down")

        monkeypatch.setattr(AnalyticsEvent.objects, "create", boom)
        with pytest.raises(RuntimeError):
            bind_external_identity("bot:max:audit-atomic", real.pk)
        # The proxy row was created inside the same transaction — it
        # rolled back too; resolution now lazily creates a FRESH,
        # unbound proxy.
        assert not User.objects.filter(
            username="bot:max:audit-atomic",
        ).exists()
        assert _audit_events("bot:max:audit-atomic") == []

    def test_strict_audit_integrity_error_rolls_back_binding(self, monkeypatch):
        """A NON-dedup IntegrityError from the audit sink (FK failure,
        a future NOT NULL, …) is a real sink failure, not a replay: in
        strict mode it must propagate — never be swallowed by the dedup
        handler — and roll the identity mutation back."""
        real = _real_user("audit-ie", "+79993330008")

        def boom(*args, **kwargs):
            raise IntegrityError("insert violates foreign key constraint")

        monkeypatch.setattr(AnalyticsEvent.objects, "create", boom)
        with pytest.raises(IntegrityError):
            bind_external_identity("bot:max:audit-ie", real.pk)
        # Binding + proxy creation rolled back with the failed audit.
        assert not User.objects.filter(username="bot:max:audit-ie").exists()
        assert _audit_events("bot:max:audit-ie") == []

    def test_rebind_after_unlink_writes_fresh_audit_rows(self):
        """bind → unlink → bind of the SAME pair is three committed
        operations and must produce THREE audit rows: a re-bind after
        an unlink is a NEW operation, not a retry — its dedup key
        differs by the generation discriminator. (Regression coverage:
        the plain deterministic key collapsed the second bind into the
        first one's row, committing a binding with no audit — exactly
        on the managed relink path AYLA-DEC-0016 §4 audits.) Same for
        a second unlink after the re-bind."""
        real = _real_user("audit-relink", "+79993330009")
        bind_external_identity("bot:max:audit-relink", real.pk)
        unlink_external_identity("bot:max:audit-relink")
        bind_external_identity("bot:max:audit-relink", real.pk)
        assert resolve_external_user("bot:max:audit-relink").pk == real.pk
        results = [
            e.payload["result"] for e in _audit_events("bot:max:audit-relink")
        ]
        assert results == ["created", "unlinked", "created"]
        # And a second unlink gets its own row too (fourth generation).
        unlink_external_identity("bot:max:audit-relink")
        results = [
            e.payload["result"] for e in _audit_events("bot:max:audit-relink")
        ]
        assert results == ["created", "unlinked", "created", "unlinked"]
        assert resolve_external_user("bot:max:audit-relink").is_proxy is True

    def test_rejected_null_actor_audit_deduped(self):
        """Rejected outcomes audit with actor=NULL — outside BOTH
        partial dedup constraints. The app-level pre-check caps a
        looping caller (e.g. a provisioning script retrying a malformed
        target) at one audit row per distinct rejection."""
        missing = uuid4()
        for _ in range(3):
            with pytest.raises(BindTargetNotFoundError):
                bind_external_identity("bot:max:audit-loop", missing)
        (event,) = _audit_events("bot:max:audit-loop")
        assert event.actor_id is None
        assert event.payload["result"] == "rejected"
        assert event.payload["reason"] == "target_not_bindable"

    def test_strict_replay_absorbed_logs_warning(self):
        """Round-3 P4: if audit history loses the opposite-direction
        row (retention / anonymisation), the generation discriminator
        collapses onto a previous generation's key. The strict replay
        is then absorbed against the EXISTING row — the strict
        guarantee still holds (a committed audit row for this exact
        operation exists) — but the reuse of an old generation's row
        must NOT be silent: it logs at WARNING.

        Logger spy instead of caplog: settings/base.py sets
        ``propagate=False`` on the ``users`` logger, so caplog's
        root-attached handler never receives these records (same
        pattern as appointments/tests/test_tasks.py)."""
        real = _real_user("audit-p4", "+79993330010")
        bind_external_identity("bot:max:audit-p4", real.pk)
        unlink_external_identity("bot:max:audit-p4")
        # Simulate retention losing the "unlinked" row: the next bind's
        # generation count drops back to 0, colliding with the FIRST
        # bind's dedup key.
        AnalyticsEvent.objects.filter(
            event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
            payload__external_user_id="bot:max:audit-p4",
            payload__result="unlinked",
        ).delete()
        with patch("users.identity_events.logger") as mock_logger:
            bind_external_identity("bot:max:audit-p4", real.pk)
        # The binding committed (resolution works)…
        assert resolve_external_user("bot:max:audit-p4").pk == real.pk
        # …the replay was absorbed against the first generation's row
        # (still exactly one "created" row)…
        results = [
            e.payload["result"] for e in _audit_events("bot:max:audit-p4")
        ]
        assert results == ["created"]
        # …and the absorption was LOUD, not silent.
        warnings = [
            c for c in mock_logger.warning.call_args_list
            if "binding_audit_replay_absorbed" in c.args[0]
        ]
        assert len(warnings) == 1


# ── Model invariants ─────────────────────────────────────────────


@pytest.mark.django_db
class TestLinkedUserModelInvariants:
    def test_real_user_cannot_carry_linked_user(self):
        real = _real_user("inv-real", "+79994440001")
        other = _real_user("inv-other", "+79994440002")
        real.linked_user = other
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                real.save(update_fields=["linked_user"])

    def test_proxy_cannot_point_at_itself(self):
        proxy = resolve_external_user("bot:max:inv-self")
        proxy.linked_user = proxy
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                proxy.save(update_fields=["linked_user"])

    def test_physical_target_delete_reverts_to_unbound(self):
        """SET_NULL on the FK: physically deleting the bound account
        reverts the proxy to unbound — resolver returns the isolated
        proxy (fail-closed), never another user's data."""
        real = _real_user("inv-delete", "+79994440003")
        bind_external_identity("bot:max:inv-delete", real.pk)
        real.delete()
        proxy = User.objects.get(username="bot:max:inv-delete")
        assert proxy.linked_user_id is None
        resolved = resolve_external_user("bot:max:inv-delete")
        assert resolved.is_proxy is True
        assert resolved.pk == proxy.pk

    def test_resolver_follows_single_hop_only(self):
        """Even if a proxy→proxy pointer were written bypassing the
        service (defence in depth), resolution takes at most one hop —
        no chain-walking, no loop."""
        inner = resolve_external_user("bot:max:chain-inner")
        outer = resolve_external_user("bot:max:chain-outer")
        inner.linked_user = _real_user("chain-real", "+79994440004")
        inner.save(update_fields=["linked_user"])
        outer.linked_user = inner
        outer.save(update_fields=["linked_user"])
        resolved = resolve_external_user("bot:max:chain-outer")
        # One hop: resolves to `inner` (an active proxy), never walks
        # through to the real account at the end of the chain.
        assert resolved.pk == inner.pk


# ── P2: username collision / row-state change ────────────────────


@pytest.mark.django_db
class TestExternalIDCollision:
    def test_real_account_username_collision_controlled_409(self):
        """If a REAL account's username already equals the external id,
        get_or_create returns that row. The bind must refuse with a
        CONTROLLED conflict + rejected audit — never an unhandled
        IntegrityError from user_linked_user_only_proxy."""
        real = _real_user(username="bot:max:collide", phone="+79995550001")
        target = _real_user(username="collide-target", phone="+79995550002")
        with pytest.raises(IdentityBindingConflictError):
            bind_external_identity("bot:max:collide", target.pk)
        real.refresh_from_db()
        assert real.linked_user_id is None
        assert real.is_proxy is False
        (event,) = _audit_events("bot:max:collide")
        assert event.payload["result"] == "rejected"
        assert event.payload["reason"] == "external_id_collision"

    def test_row_turned_non_proxy_before_lock_controlled_409(self):
        """The is_proxy check is repeated on the LOCKED row: a row that
        stopped being a proxy between lookup and lock (unexpected state
        change) gets the same controlled refusal, not a constraint
        violation."""
        proxy = resolve_external_user("bot:max:flipped")
        User.objects.filter(pk=proxy.pk).update(is_proxy=False)
        target = _real_user(username="flip-target", phone="+79995550003")
        with pytest.raises(IdentityBindingConflictError):
            bind_external_identity("bot:max:flipped", target.pk)
        (event,) = _audit_events("bot:max:flipped")
        assert event.payload["reason"] == "external_id_collision"


# ── P2: tombstone lifecycle — audited managed unlink ─────────────


@pytest.mark.django_db
class TestUnlinkExternalIdentity:
    def test_unlink_clears_binding_and_audits(self):
        real = _real_user("unlink-a", "+79996660001")
        bind_external_identity("bot:max:unlink", real.pk)
        proxy, was_bound = unlink_external_identity(
            "bot:max:unlink", request_id="req-unlink",
        )
        assert was_bound is True
        assert proxy.linked_user_id is None
        # Resolver is back to the isolated proxy.
        assert resolve_external_user("bot:max:unlink").is_proxy is True
        events = _audit_events("bot:max:unlink")
        assert [e.payload["result"] for e in events] == ["created", "unlinked"]
        unlink_event = events[-1]
        assert unlink_event.payload["operation"] == "unlink_external_identity"
        assert unlink_event.payload["reason"] == "support_unlink"
        assert unlink_event.payload["target_user_id"] == str(real.pk)
        assert unlink_event.payload["request_id"] == "req-unlink"
        # The FORMER target is the audit actor: "when was this client
        # unbound" stays answerable via the (actor, -created_at) index.
        assert unlink_event.actor_id == real.pk

    def test_unlink_unbound_proxy_is_idempotent_noop(self):
        proxy = resolve_external_user("bot:max:unlink-noop")
        same, was_bound = unlink_external_identity("bot:max:unlink-noop")
        assert was_bound is False
        assert same.pk == proxy.pk
        (event,) = _audit_events("bot:max:unlink-noop")
        assert event.payload["result"] == "unlinked"
        assert event.payload["reason"] == "already_unbound"
        # No target ever resolved — actor stays NULL (best-effort row).
        assert event.actor_id is None

    def test_unlink_unknown_identity_404_and_audited(self):
        with pytest.raises(ExternalIdentityNotFoundError):
            unlink_external_identity("bot:max:unlink-ghost")
        (event,) = _audit_events("bot:max:unlink-ghost")
        assert event.payload["result"] == "rejected"
        assert event.payload["reason"] == "external_identity_unknown"

    def test_soft_deleted_target_tombstone_then_unlink_then_rebind(self):
        """Full lifecycle: bind → target soft-deleted (binding void for
        resolution, pointer kept as tombstone; rebind still 409) →
        audited unlink → identity bindable to the new account."""
        first = _real_user("tomb-a", "+79996660002")
        bind_external_identity("bot:max:tomb", first.pk)
        first.is_active = False
        first.deleted_at = timezone.now()
        first.save(update_fields=["is_active", "deleted_at"])
        # Void for resolution, tombstone kept: rebind conflicts.
        assert resolve_external_user("bot:max:tomb").is_proxy is True
        second = _real_user("tomb-b", "+79996660003")
        with pytest.raises(IdentityBindingConflictError):
            bind_external_identity("bot:max:tomb", second.pk)
        # Managed unlink clears the tombstone; rebind succeeds.
        _, was_bound = unlink_external_identity("bot:max:tomb")
        assert was_bound is True
        bind_external_identity("bot:max:tomb", second.pk)
        assert resolve_external_user("bot:max:tomb").pk == second.pk

    def test_unlink_audit_failure_rolls_back(self, monkeypatch):
        """The unlink audit is STRICT and in-transaction too: a broken
        audit sink leaves the binding intact."""
        real = _real_user("unlink-atomic", "+79996660004")
        bind_external_identity("bot:max:unlink-atomic", real.pk)

        def boom(*args, **kwargs):
            raise RuntimeError("audit sink down")

        monkeypatch.setattr(AnalyticsEvent.objects, "create", boom)
        with pytest.raises(RuntimeError):
            unlink_external_identity("bot:max:unlink-atomic")
        proxy = User.objects.get(username="bot:max:unlink-atomic")
        assert proxy.linked_user_id == real.pk
