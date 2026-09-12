"""Personal-data access audit — the 152-ФЗ access journal (owner §96).

Its own app, deliberately, and not a table inside ``users`` or a row in
``analytics``:

* **Analytics is not an audit.** Owner ruling §96 — analytics events serve a
  different purpose, may be aggregated, pruned, or have their schema changed
  under the assumption that no single row matters. An access journal is the
  opposite: the single row is the whole point.
* **A log is not an audit either.** The catalogue's ``users`` logger is
  configured ``propagate: False`` to a console handler. Whatever it records
  survives exactly as long as log rotation — which is what §7's
  ``audit sensitive access`` exists to fix.
* **The journal is itself sensitive data.** Who read whose personal data,
  and when, is a second personal record about the same people. Giving it its
  own app gives it its own permission namespace, so "may read the journal"
  is grantable independently of "may read the users table" instead of riding
  along with it.

Analytics may still receive the same facts as metrics. It is not the audit
and never counts as one.
"""
from __future__ import annotations

from django.apps import AppConfig


class PrivacyAuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "privacy_audit"
    verbose_name = "Журнал доступа к персональным данным"
