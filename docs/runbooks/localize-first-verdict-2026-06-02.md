# Localize-first verdict — FoodScan (152-ФЗ ст. 18 п. 5)

> **Pre-pilot legal gate.** Verification only — no code changes here.
> **Source of question:** founder — confirm РФ-первичка (RF-side
> structured record) exists BEFORE photo bytes cross the border via
> OpenAI Vision. Feeds "Путь B" mechanism.
> **Audited:** 2026-06-02 (Alpha).
> **Companion of:** Opt-2 implementation PR #188 (merged 2026-06-02).

## TL;DR

| Bound | Status |
|---|---|
| RF-side `FoodScan` row committed BEFORE cross-border OpenAI Vision call | ✅ **EXISTS** |
| Public endpoint `POST /api/v1/nutrition/scan/` | ✅ localize-first |
| Internal endpoint `POST /api/v1/nutrition/internal/scan/` | ✅ localize-first |
| Pin tests guarding the invariant | ✅ both endpoints |
| RF-localisation of the storage layer (MinIO) | 🟡 ops responsibility — out of code scope |

**Verdict:** **РФ-первичка ЕСТЬ.** The structured record (`FoodScan` row
linking `user` → `image` → `caption`) is committed to the RF-located
Postgres BEFORE the photo bytes are transmitted to OpenAI Vision. The
invariant is implemented identically on both public (`FoodScanView`)
and service-to-service (`InternalFoodScanView`) entrypoints, and is
pinned by two adversarial tests that observe row count from inside the
mocked router (any regression that moves `scan.save()` after
`router.scan()` will turn the count to `[0]` and fail the suite).

---

## Audit method

### 1. Code path inspection

`nutrition/views.py::FoodScanView.post` (lines 110–205):

```python
# 1. Read photo into memory (boundary — bytes are still in RF process).
image_bytes = image_file.read()

# 2. Instantiate the row (NOT committed yet).
scan = FoodScan(user=request.user)

# 3. Push photo to MinIO via Django's S3Boto3 backend.
scan.image.save(f"{scan.id}.jpg", ContentFile(image_bytes), save=False)

# 4. 152-ФЗ ст. 18 п. 5 — localize-first gate.
#    Commit the structured RF-side record BEFORE the cross-border call.
scan.save()  # <-- RF-первичка появилась здесь

# 5. Cross-border call — bytes leave RF here (OpenAI us-east-1).
router = FoodScannerRouter()
outcome = router.scan(image_bytes, ...)
```

`nutrition/views.py::InternalFoodScanView.post` (lines 235–325) follows
the same shape, with `resolve_external_user` resolving the bot's
`X-External-User-ID` → `ProxyUser` (also RF-committed) BEFORE step 2.

### 2. Pin tests

Two adversarial tests guard the invariant. Both snapshot the DB row
count from inside a mocked `FoodScannerRouter.scan` side-effect — the
exact moment the cross-border call would normally fire:

* `nutrition/tests/test_views.py::TestLocalizeFirst152FZ::test_scan_row_exists_in_db_before_vendor_call`
  — public endpoint (JWT-authenticated client).
* `nutrition/tests/test_internal_food_scan.py::test_localize_first_scan_row_exists_before_vendor_call`
  — internal endpoint (service-token authenticated, ProxyUser actor).

Both assert `observed_rows_at_router_call == [1]`. Any regression that
moves `scan.save()` below `router.scan()` produces `[0]` and trips a
failure message naming the file and the fix.

### 3. CI confirmation

* Latest dev CI/CD run `26836151129` (2026-06-02) — Run tests step
  includes the pin tests → ✅ green.
* Latest dev smoke run `26836868662` (2026-06-02, post web container
  recovery) — runtime exercise against deployed code → ✅ green.

---

## Storage layer (MinIO) — ops note

The photo file itself is written to MinIO via Django's
`S3Boto3Storage` backend BEFORE the cross-border call (`image.save(...,
save=False)` in step 3 above). The bytes hit MinIO during step 3 and
the row commits during step 4 — both are RF-side operations IF the
MinIO instance is RF-located.

**Code-level invariant: satisfied.** The view always writes to MinIO
before calling the vendor. **Operational invariant: ops responsibility.**
The `MINIO_ENDPOINT` env var on prod/dev/pilot deployments must point
at an RF-located MinIO node. This is configured at infra layer
(`djangoProject/settings/{prod,dev}.py` reads `MINIO_ENDPOINT` from
env); the code makes no assumption about geography. Ops audit of the
deployment topology is required to close this leg of ст. 18 п. 5 at
the operational level.

---

## Verification commands (reproducible)

Run these against `dev` to confirm the audit findings stay valid:

```bash
# 1. Both endpoints have scan.save() BEFORE router.scan()?
grep -n "scan\.save\(\)\|router\.scan(" nutrition/views.py
#   → expect: scan.save() line numbers strictly LESS than the
#     immediately following router.scan() line numbers for both
#     FoodScanView.post and InternalFoodScanView.post.

# 2. Pin tests exist and pass?
pytest nutrition/tests/test_views.py::TestLocalizeFirst152FZ \
       nutrition/tests/test_internal_food_scan.py::TestScanFlow::test_localize_first_scan_row_exists_before_vendor_call -v
#   → expect: 2 passed.

# 3. No new vendor calls bypass the localize-first window?
grep -rn "openai\|YandexVision\|claude.*scan\|anthropic" nutrition/ --include="*.py"
#   → expect: all hits go through FoodScannerRouter, which is only
#     invoked from views.py AFTER scan.save().
```

---

## Verdict for pre-ship gate

* ✅ Code-level invariant — `scan.save()` strictly precedes
  `router.scan()` on both endpoints.
* ✅ Pin tests — adversarial, will flip on any regression.
* ✅ CI green on dev — tests + smoke both passing 2026-06-02.
* 🟡 Storage layer (MinIO) — ops must confirm RF-located MinIO endpoint
  on pilot deployment. Code does not constrain this; env var does.
* ✅ **Pilot ship-ready** on this dimension — РФ-первичка существует
  ДО cross-border вызова.

**Mechanism Пути B:** can rely on the invariant that a queryable RF-
side `FoodScan` row exists for every scan, with the photo committed to
MinIO and the structured record committed to Postgres, BEFORE any
foreign-located service receives the bytes. If Путь B needs to
reconcile or replay scans from RF state alone, the data is there.

Refs: 152-ФЗ ст. 18 п. 5, founder Opt-2 (pre-ship gate, 2026-06-02),
implementation PR #188, R-2 retention audit (sibling gate,
`docs/runbooks/data-retention-audit-2026-06-02.md`).
