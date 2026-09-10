"""One place that records every access on the personal-data surface.

Eight routes, one recorder. The alternative — a call at the top of each
handler — is eight chances to forget, and the ninth route arrives next month
looking audited because its neighbours are.

The mixin also owns the ORDERING that owner §96 requires, and the ordering
differs by method for a reason that is not stylistic:

* a **read** is recorded after the body is built and before it leaves the
  process, so a failed journal write can turn into a refusal with nothing
  disclosed;
* a **write or erasure** shares ONE transaction with the journal row, so a
  failed journal write rolls the effect back. There is no state where data
  was erased and the erasure went unrecorded.

Whether a failed write actually refuses is the owner's boundary from §107 and
lives in :mod:`privacy_audit.policy`: only disclosing and destroying
operations stop. Everything else is queued —
:mod:`privacy_audit.spool` — because our outage must not stop a person acting
on their own data. The ordering above still matters for the queued ones: it is
what keeps the queued record consistent with what actually happened.

Refusals ride the same path: the decision the permission made is on the
request, so a denial is journalled with its own reason instead of being
reconstructed from a 403 — and a denial whose row cannot be written is queued
rather than dropped.
"""
from __future__ import annotations

import logging

from django.db import transaction

from privacy_audit import outcome as authz_outcome
from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.services import AuditUnavailable, basis_from, record_or_queue
from users.response import error_response

logger = logging.getLogger("privacy_audit")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuditedPersonalDataAccess:
    """Mix in FIRST, before ``APIView``, so ``dispatch`` wraps the view's.

    Subclasses declare what the route touches::

        class InternalPersonalDataExportView(AuditedPersonalDataAccess, APIView):
            audit_object_category = PersonalDataAccessLog.ObjectCategory.PERSONAL_DATA
            audit_operations = {"GET": PersonalDataAccessLog.Operation.EXPORT}

    A method missing from ``audit_operations`` is a route that would go
    unrecorded, so it is refused rather than served — the same deny-by-default
    stance the authorization check takes, applied to the journal.
    """

    #: ``PersonalDataAccessLog.ObjectCategory`` value.
    audit_object_category: str = ""
    #: HTTP method -> ``PersonalDataAccessLog.Operation`` value.
    audit_operations: dict = {}
    #: URL kwarg naming the subject. Shared with the permission class, which
    #: authorises against the same kwarg — one name, one meaning.
    subject_url_kwarg: str = ""

    def dispatch(self, request, *args, **kwargs):
        unsafe = request.method not in _SAFE_METHODS
        try:
            if unsafe:
                with transaction.atomic():
                    response = super().dispatch(request, *args, **kwargs)
                    self._journal(response)
            else:
                response = super().dispatch(request, *args, **kwargs)
                self._journal(response)
        except AuditUnavailable:
            return self._refuse_unaudited()
        return response

    # -- internals ---------------------------------------------------------

    def _journal(self, response) -> None:
        """Write the row for this request, or raise ``AuditUnavailable``."""
        request = getattr(self, "request", None)
        if request is None:
            # DRF failed before ``initialize_request`` — no permission ran,
            # so no handler ran either. Nothing was accessed; nothing to
            # record. Logged because "no outcome" must never be silent.
            logger.warning("privacy_audit.no_request_to_journal")
            return

        decision = authz_outcome.read(request)
        if decision is None:
            # The permission never reached a verdict — e.g. the method is not
            # allowed on this route, refused before authorization. The handler
            # did not run, so nothing was reached.
            if response.status_code not in (405, 415):
                logger.error(
                    "privacy_audit.outcome_missing path=%s status=%s — a "
                    "request reached a handler without an authorization "
                    "verdict; this route may be unguarded",
                    request.path, response.status_code,
                )
            return

        if not decision.auditable:
            # Unauthenticated knock: no actor, no role, no tenant to record.
            # Stays in the log — see ``privacy_audit.outcome``.
            return

        operation = self.audit_operations.get(request.method)
        if not operation:
            logger.error(
                "privacy_audit.operation_undeclared path=%s method=%s view=%s",
                request.path, request.method, type(self).__name__,
            )
            raise AuditUnavailable(
                f"{type(self).__name__} declares no audit operation for "
                f"{request.method}"
            )

        # ``record_or_queue`` owns the owner's boundary (§107): a disclosing or
        # destroying operation that cannot be journalled raises and is refused
        # by the caller below; anything else is queued, and a denial is always
        # queued rather than dropped — an attempt to reach somebody else's
        # subject has to arrive in the journal even if the journal was down
        # when it happened.
        stored = record_or_queue(
            caller_purpose=decision.caller_purpose,
            actor=decision.actor,
            object_id=self.kwargs.get(self.subject_url_kwarg),
            operation=operation,
            object_category=self.audit_object_category,
            result=(
                PersonalDataAccessLog.Result.ALLOWED if decision.allowed
                else PersonalDataAccessLog.Result.DENIED
            ),
            actor_named=decision.actor_named,
            denial_reason=decision.reason,
            basis=basis_from(request),
            request_id=str(getattr(request, "request_id", "") or ""),
        )
        if stored != "written":
            logger.warning(
                "privacy_audit.record_deferred how=%s path=%s operation=%s "
                "result=%s", stored, request.path, operation,
                "allowed" if decision.allowed else "denied",
            )

    def _refuse_unaudited(self):
        """503 — the access did not happen because it could not be recorded.

        Deliberately not a 500: nothing is broken about the request. The
        service is temporarily unable to perform an operation whose
        precondition is a durable record, which is what 503 means.
        """
        logger.error(
            "privacy_audit.access_refused_unaudited path=%s method=%s",
            getattr(self.request, "path", "?"), getattr(self.request, "method", "?"),
        )
        response = error_response(
            "SERVICE_UNAVAILABLE",
            "Access to personal data requires a durable audit record, and the "
            "audit journal is unavailable.",
            status_code=503,
        )
        return self.finalize_response(self.request, response, *self.args, **self.kwargs)
