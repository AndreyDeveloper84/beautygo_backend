# Data retention audit — R-2 (appointments + payments)

> **Pre-pilot gate.** Verification only — no code changes here.
> **Source of question:** founder R-2 — confirm `≥7 years` (legal floor)
> **AND** `≤7 years` (no over-retention).
> **Audited:** 2026-06-02 (Alpha).

## TL;DR

| Bound | Status |
|---|---|
| `≥7 years` legal floor (152-ФЗ §5 ч.6 + ФЗ-402 бухучёт) | ✅ Trivially satisfied |
| `≤7 years` ceiling (152-ФЗ §5 ч.7 — over-retention) | ⚠️ Gap — no deletion task exists |

**Pilot impact:** **NONE**. Pilot data is <1 year old at launch (2026-07-15) — the over-retention bound becomes load-bearing only post-2033. The gap is a **post-pilot architecture task**, not a pre-pilot ship blocker.

**Recommendation:** Document the gap (this file), open a tracking ticket for the post-pilot retention task, ship pilot.

---

## Audit method

Searched the codebase for any deletion / purge / TTL logic touching `Appointment` or `Payment`:

```bash
grep -rln "purge|cleanup|delete.*old|TTL|days.*=.*365" appointments/ payments/
```

Findings:

* `appointments/tasks.py::purge_expired_idempotency_keys` — 24h TTL on
  the `IdempotencyKey` table (replay-protection cache). Does **NOT**
  touch `Appointment` or `Payment` rows.
* `appointments/infrastructure/idempotency.py:162` — drops expired
  idempotency keys at first use. Same scope.
* `payments/services.py` — no deletion path.
* `appointments/application/services/cancel_reschedule_service.py` —
  state transition (status → `cancelled`), **NOT** row deletion. The
  row persists; only its `status` field changes.

**Conclusion:** No automated deletion of `Appointment` or `Payment`
rows exists. The cascade behaviour on related FKs is all `PROTECT`
(client / specialist / tenant / service / appointment) — meaning a
parent record CANNOT be deleted while child appointments/payments
exist. The rows therefore persist indefinitely until manual ops
intervention (admin `DELETE`, raw SQL).

---

## Floor (`≥7 years`)

**Met by absence of deletion.** Rows live forever in the current
implementation. Legal floor — 152-ФЗ §5 ч.6 plus ФЗ-402 бухучёт
(4 years minimum for payment records, 5 years for бухгалтерская
отчётность) — is satisfied with margin.

* `Appointment` row: persists indefinitely → ≥7 years ✓
* `Payment` row: persists indefinitely → ≥7 years ✓
* YooKassa-side records (provider): YooKassa retains per their
  own policy; cross-reference via `Payment.provider_payment_id`.

**Backup strategy:** out of scope for this audit. Confirm with
ops that DB backups themselves are retained ≥7y.

---

## Ceiling (`≤7 years`) — **GAP**

**152-ФЗ §5 ч.7** (хранение в форме, позволяющей определить субъекта,
не дольше чем требуется по целям): the operator must define a
maximum retention period and delete (or anonymise) once it's reached.
We have no such mechanism today.

### Pilot risk: **LOW**

* Pilot launch 2026-07-15.
* Oldest possible Appointment / Payment row at pilot: ~2026-04 (early
  internal smoke) — ~1 year old.
* The 7-year ceiling kicks in 2033-04 at the earliest.
* No legal exposure during pilot (max 1 year of data).

### Post-pilot risk: **MEDIUM**

* Continuous growth without a retention task means the table grows
  monotonically. Performance impact is the proximate concern — the
  legal concern is downstream and recoverable (one deletion task
  catches up).
* By 2030 we should have the retention task running.

### Recommended fix (post-pilot)

1. Add `appointments.tasks.purge_old_appointments` Celery beat task,
   monthly cadence.
2. Filter: `Appointment.objects.filter(status__in=TERMINAL,
   start_datetime__lt=now() - relativedelta(years=7))`.
3. **Anonymise rather than delete** — the row's existence matters
   for billing reconciliation (ФЗ-402); the personal data
   (client FK, specialist FK denormalised fields, notes,
   cancellation_reason) is what must be cleared.
4. Same shape for `Payment` — anonymise `provider_payment_id`,
   `provider_client_secret`, clear `last_webhook_event_id`.
5. Audit log entry per anonymisation operation (operator-visible).

**Out of scope for pilot.** Track as a post-pilot legal-compliance
ticket — recommended due ~2027-Q1 (one year out from pilot launch,
plenty of buffer before the 7-year boundary lands on real data).

---

## Related considerations (not R-2 scope)

These came up during the audit but are not the R-2 question:

* **User account deletion (152-ФЗ §14 — право на удаление):** Each
  `User.delete()` cascades through `PROTECT` FKs → operationally
  impossible without an explicit anonymisation flow. Track
  separately.
* **FoodScan retention:** Image files in MinIO + metadata in
  `nutrition.FoodScan` — same indefinite-storage shape. Same gap,
  same post-pilot timeline.
* **Notification rows:** Slice N4 retention beat task exists
  (`notifications.tests.test_retention_tasks`), but it's about
  *user engagement retention*, NOT data deletion. Same `purge` story.

---

## Verification commands (reproducible)

Run these against `dev` to confirm the audit findings stay valid:

```bash
# 1. Any task that deletes Appointment or Payment rows?
grep -rn "Appointment\.objects.*delete\|Payment\.objects.*delete" \
  appointments/ payments/ --include="*.py"
#   → expect: zero hits in production code

# 2. Any TTL-style field on the two models?
grep -n "expires_at\|deleted_at\|purge_after" \
  appointments/models.py payments/models.py
#   → expect: only Appointment.idempotency_key shape, not data rows

# 3. Any Celery beat schedule entry that touches these tables?
grep -n "schedule\|crontab" appointments/tasks.py payments/services.py
#   → expect: only purge_expired_idempotency_keys + outbox dispatch
```

---

## Verdict for pre-ship gate

* ✅ `≥7 years` legal floor — satisfied.
* ⚠️ `≤7 years` ceiling — gap documented, post-pilot task tracked.
* ✅ **Pilot ship-ready** on this dimension. The retention task
  doesn't need to exist by 2026-07-15; it needs to exist by ~2030.

Refs: 152-ФЗ §5 ч.6+ч.7, ФЗ-402 бухучёт (4-5 years), founder R-2
(pre-ship gate, 2026-06-02).
