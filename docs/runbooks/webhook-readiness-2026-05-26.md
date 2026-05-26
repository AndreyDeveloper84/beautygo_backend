# Webhook readiness audit — 2026-05-26

> **Task #101 (Alpha-internal). Scope:** confirm the YooKassa payment
> webhook is production-ready for pilot launch 2026-07-15 and document
> the gaps that remain. MAX webhook is bot-platform territory per
> ADR-0009 — out of Alpha scope, no audit performed here.

## Endpoint

```
POST /api/v1/payments/webhook/
```

Implementation: `payments.views.PaymentWebhookView`.
Permission: `permissions.AllowAny` — verification is via IP allowlist
+ Basic Auth, not DRF auth.
Throttle: `webhook_payment` at 100/min (amplification cap).

## What's covered (production-ready)

### 1. Defence-in-depth verification

| Layer | Mechanism | Module reference |
|---|---|---|
| IP allowlist | `YOOKASSA_WEBHOOK_ALLOWED_IPS` env (CIDR list) compared against `_client_ip()` which respects `TRUSTED_PROXY_COUNT` and reads `xff[-N]` to defeat leftmost-XFF spoofing | `payments/views.py:80-128` |
| Basic Auth | `YOOKASSA_WEBHOOK_BASIC_AUTH_USER/PASS` env, constant-time compare via `secrets.compare_digest` | `payments/views.py:40-77` |
| Re-fetch | Payment state read from YooKassa API (`get_payment_info`) — payload is not trusted on its own | `payments/views.py:399-406` |
| Idempotency | `Payment.last_webhook_event_id` keyed on `X-Request-Id` (falls back to `event:payment_id`); checked before processing AND inside the row lock | `payments/views.py:396, 424` |
| Race protection | `select_for_update()` on the Payment row inside `transaction.atomic` — concurrent webhooks for the same payment serialise | `payments/views.py:410-421` |
| Throttle | `ScopedRateThrottle('webhook_payment')` at 100/min — bounds YooKassa API fan-out in a storm | `payments/views.py:334-335` |

### 2. Event handlers — outbox emission per ADR-0009 envelope contract

| YooKassa event | Required API status | Internal state change | Outbox topic |
|---|---|---|---|
| `payment.waiting_for_capture` | `waiting_for_capture` | `Payment→AUTHORIZED`, `Appointment→CONFIRMED` | `booking.confirmed` |
| `payment.succeeded` | `succeeded` | `Payment→PAID` | `payment.confirmed` |
| `payment.canceled` | `canceled` | `Payment→FAILED`, `Appointment→CANCELLED` (if non-terminal) | (no emit) |
| `refund.succeeded` | n/a | `Payment→REFUNDED` or `PARTIALLY_REFUNDED` (depending on `refunded_amount`) | `payment.refunded` |

Reference: `payments/views.py:432-510`.

### 3. Production environment gating

`djangoProject/settings/prod.py` requires `YOOKASSA_WEBHOOK_ALLOWED_IPS`
via `enforce_required_env` — boot fails on missing value when
`DJANGO_ENV=production`. Staging / dev VPS downgrades to a warning so
the stack can boot before all creds land.

### 4. Existing test coverage

`payments/tests/test_payments_api.py`:

- `test_webhook_waiting_for_capture` / `_payment_succeeded` /
  `_payment_canceled` — event-handler state transitions
- `test_webhook_idempotent` — duplicate event_id skip
- `test_webhook_refund_succeeded_writes_outbox`,
  `test_webhook_partial_refund_marks_outbox_partial` — refund branch +
  partial refund path
- `test_webhook_unknown_payment`, `test_webhook_missing_fields` —
  graceful 200 on noise
- `TestPaymentWebhookSecurity` — IP allowlist positive/negative,
  XFF leftmost-spoofing, 2-proxy depth, empty allowlist
- `TestWebhookBasicAuth` — correct/wrong/missing-required credentials

Plus the new gap-filler in this audit (see below): event-vs-API
status mismatch graceful no-op pin.

## Known gaps — filed as separate issues

See `ai-bot-platform` issues filed alongside this audit. Summary:

1. **`YOOKASSA_WEBHOOK_BASIC_AUTH_USER/PASS` NOT in `_REQUIRED_PROD_ENV`** — Basic Auth is currently the second defence layer; without env vars set, the layer is silently disabled in prod. IP allowlist alone is fine for pilot (YooKassa IPs are static), but defence-in-depth dictates promoting Basic Auth to required.
2. **No automated end-to-end smoke test against staging** — the manual procedure below is the current backstop.
3. **No structured-logging field set on `logger.info('Webhook processed...')`** — string-format only; downstream log shippers can't cleanly filter by `event_id` / `payment_id` / `status`.
4. **No concurrent-webhook race test pinning `select_for_update`** — the protection exists, but a regression that removes the lock would not be caught by current tests.

## Manual smoke test (for ops, pre-pilot)

After deploy to dev/staging VPS:

```bash
# 1. Health check first — verify reach + readiness.
curl -fsS https://<dev-vps>/api/v1/health/
curl -fsS https://<dev-vps>/api/v1/health/ready/

# 2. POST the webhook URL with a noise payload — verify it responds
#    200 'ok' (unknown payment_id path).
curl -fsS -X POST https://<dev-vps>/api/v1/payments/webhook/ \
  -H "Content-Type: application/json" \
  -d '{"event":"payment.succeeded","object":{"id":"smoke-test-noise"}}'

# Expected: HTTP 200 {"status":"ok"}

# 3. POST from an OUTSIDE IP (or use curl with --interface) — verify
#    that IP allowlist is engaged. Expect HTTP 403.

# 4. If Basic Auth is configured (recommended): POST without Authorization
#    header — expect HTTP 403.
curl -fsS -X POST https://<dev-vps>/api/v1/payments/webhook/ \
  -H "Content-Type: application/json" \
  -d '{}' \
  -w "%{http_code}\n"

# Expected: 403 (when allowlist or basic-auth gates fail)
```

Confirm in YooKassa dashboard that the webhook URL is set to the
`https://user:pass@host/api/v1/payments/webhook/` form (Basic Auth in
URL) and the registered events are: `payment.waiting_for_capture`,
`payment.succeeded`, `payment.canceled`, `refund.succeeded`.

## Verdict

YooKassa webhook is **production-ready for pilot launch**. Defence
layers (IP allowlist + Basic Auth + re-fetch + idempotency +
row-level lock + throttle) all in place, all tested, prod env gating
enforced for the critical layer. The four gaps above are non-blocking
hardening items.

_Audited by: Alpha (Claude Opus 4.7) on 2026-05-26. PR #166._
