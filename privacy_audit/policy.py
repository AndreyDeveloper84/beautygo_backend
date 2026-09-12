"""Which operations stop when the journal stops — owner ruling, 10.09.2026.

The first implementation of §96 applied fail-closed to the whole personal-data
surface, on the reasoning that "inability to record an access is inability to
access" is a principle. The owner narrowed it, and the narrowing has a rule
behind it worth keeping in view:

    **The boundary runs along what the operation DOES to the data, not along
    whose data it touches.** Personal data is also read by the person it
    belongs to.

So an ordinary turn of conversation — a person speaking about themselves,
with their own data — is not a disclosure and not a destruction, and stopping
it because our journal is unavailable punishes the person for our outage. The
owner's words: such operations «не должны останавливать весь продукт из-за
временной недоступности аудита; для них допустима гарантированная очередь с
последующей записью».

Operations that DO stop, per the ruling: экспорт, удаление, массовый просмотр,
просмотр сотрудником чувствительных профилей или переписки, изменение
согласий и прав доступа. Three of the five exist on this surface today; the
other two (mass view, staff viewing) will arrive with slice B-2.2 and the
salon surfaces, and the set below is where they get added.

Source, read rather than retold::

    ai-bot-platform  docs/OPEN_DECISIONS.md  §107
    dev = 883b75390be7e501d279419026c64215e4bdaa70

### Why §108 does not save the stricter version

The neighbouring ruling §108 says a decision sets a LOWER bound: «строже —
оставляем, слабее — приводим к решению», so an implementation that overshot
is normally kept. It does not apply here, and the difference is worth naming
because the next reader will find §108 first and wonder.

§108 covers overshoot that lets the system distinguish MORE states. This
overshoot distinguished nothing extra — it applied one rule to operations the
owner had deliberately excluded, and §107 gives the reason directly:
«останавливать его из-за недоступности журнала значит наказывать человека за
нашу поломку». Stricter was not better here; it was wrong in a way the ruling
names. So this is the «слабее — приводим к решению» direction read correctly:
we are not weakening the guarantee, we are removing a stop the owner ruled
against, and the record still lands through the queue.

### The queue is an obligation, not a softening

What changed on 12.09.2026 — owner decision D1 (OD-AUDIT-SPOOL, A)
-------------------------------------------------------------------

The queue is gone. The owner ruled that a disk spool is «второй sensitive
store» — a second file of who-reached-whom lying on a disk — and that an
export without a guaranteed audit trail does not happen. So the operations
above STOP (503, nothing disclosed, nothing destroyed), exactly as before.

For everything else §107 still stands — «наказывать человека за нашу
поломку» is not allowed — but the mechanism that made "not stopped" also
"eventually recorded" no longer exists. What is left for those operations
is :func:`privacy_audit.services.record_or_lose`: the operation proceeds and
the loss is written at ERROR with the operation and subject id, as a
counted, named blind spot — never as a silent nothing. Whether that blind
spot is acceptable, or whether those operations should stop too now that
the queue is gone, is a question the two rulings leave open between them;
it is registered for the owner (OD register) rather than decided here.
"""
from __future__ import annotations

from privacy_audit.models import PersonalDataAccessLog

_Op = PersonalDataAccessLog.Operation

#: Operations that disclose or destroy. If the journal cannot record one of
#: these, the operation does not happen.
#:
#: ``ERASE_CONTEXT`` is here and ``WRITE_CONTEXT`` is not. That is the one
#: judgement call in this set, and it was made on a specific ground, not on a
#: view of how harmful an overwrite is:
#:
#:     ``WRITE_CONTEXT`` is queued **because the owner's list is closed** —
#:     not because overwriting is harmless.
#:
#: The list names удаление, and names изменение only for consents and access
#: rights, not for declared preferences. An overwrite does lose the previous
#: value, so the pull to call it destructive is real; extending a closed list
#: by analogy would be deciding for the owner. If it ever turns out that the
#: history of preferences matters, that is a NEW decision — visible as one —
#: rather than a quiet edit here.
FAIL_CLOSED_OPERATIONS: frozenset[str] = frozenset({
    _Op.EXPORT,        # раскрывает
    _Op.DELETE,        # разрушает
    _Op.ERASE_CONTEXT,  # разрушает — второй маршрут к той же erase_personal_context
    # Заявка на удаление аккаунта — начало разрушения (DRF-1699): без записи
    # о том, кто его запустил, разрушение не начинается.
    _Op.DELETION_REQUEST_CREATE,
})


def stops_when_unauditable(operation: str) -> bool:
    """True when a failed journal write must cancel the operation."""
    return operation in FAIL_CLOSED_OPERATIONS
