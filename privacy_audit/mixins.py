"""One place that records every access on the personal-data surface.

Eight routes, one recorder. The alternative — a call at the top of each
handler — is eight chances to forget, and the ninth route arrives next month
looking audited because its neighbours are.

The mixin also owns the ORDERING that owner §96 requires, and the ordering
differs by method for a reason that is not stylistic:

* a **read** is recorded after the body is built and before it leaves the
  process, so a failed journal write turns into a refusal and nothing is
  disclosed;
* a **write or erasure** shares ONE transaction with the journal row, so a
  failed journal write rolls the effect back. There is no state where data
  was erased and the erasure went unrecorded.

Refusals ride the same path: the decision the permission made is on the
request, so a denial is journalled with its own reason instead of being
reconstructed from a 403.
"""
from __future__ import annotations

import logging

from django.db import transaction

from privacy_audit import outcome as authz_outcome
from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.services import AuditUnavailable, basis_from, record_access
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

        try:
            record_access(
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
        except AuditUnavailable:
            if decision.allowed:
                raise
            # A refusal cannot fail closed any harder than it already is, and
            # turning it into a 503 would tell the caller "try again later"
            # about a request that was refused on the merits — a false answer
            # to a true question. The refusal stands; the fact that it went
            # unrecorded is an operational alarm, not the caller's problem.
            logger.error(
                "privacy_audit.denial_unrecorded path=%s reason=%s subject=%s "
                "— the refusal held, the journal did not",
                request.path, decision.reason,
                self.kwargs.get(self.subject_url_kwarg),
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
