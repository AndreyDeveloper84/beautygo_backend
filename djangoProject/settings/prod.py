import os

from .base import *

from core.env_strictness import enforce_required_env, enforce_url_env

# Security
DEBUG = False
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',')


# Fail-fast on env vars whose absence silently degrades a security control
# in code that would otherwise look like it's working.
#
# - GOOGLE_CLIENT_ID / APPLE_CLIENT_ID: without these,
#   social_auth.verify_google_token / verify_apple_token skip the "aud"
#   claim check and accept tokens issued for any other OAuth client.
#   That's the confused-deputy account-takeover path.
# - YOOKASSA_WEBHOOK_ALLOWED_IPS: without this, the webhook IP allowlist
#   logs a warning but accepts every source. An attacker who can guess a
#   provider_payment_id can replay events; the re-fetch + idempotency
#   layers narrow blast radius but don't close it.
# - AYLA_INTERNAL_API_TOKEN: handoff Block A → A5. Cross-service Bearer
#   token used by bot-platform AylaPaymentsClient / AylaBookingClient
#   (codex P0-3) and IsBotServiceWithVerifiedClient permission. Empty
#   value silently rejects every internal request as 401 — bot-side
#   live-mode payment/booking calls degrade to opaque "service down"
#   errors with no obvious root cause from logs. Pinning it here gives
#   the operator a single error message at boot instead of paged-at-
#   midnight payment failures.
#
# Strictness gated by DJANGO_ENV: real production raises on missing
# values, staging / dev VPS environments downgrade to warnings so the
# stack can boot before all credentials are provisioned. dev.py is
# never gated this way — local work uses placeholder values freely.
_REQUIRED_PROD_ENV = (
    "GOOGLE_CLIENT_ID",
    "APPLE_CLIENT_ID",
    "YOOKASSA_WEBHOOK_ALLOWED_IPS",
    "AYLA_INTERNAL_API_TOKEN",
)
_missing_prod = [name for name in _REQUIRED_PROD_ENV if not os.environ.get(name)]
enforce_required_env(
    _missing_prod,
    "each disables a defence layer when unset; see djangoProject/settings/"
    "base.py for what each one gates",
)

# DRF-1244 — presence is not fitness.
#
# The gate above proves a variable was *set*. It says nothing about the
# value: `AYLA_PUBLIC_BASE_URL=да`, `=TODO`, a half-pasted `=https://`
# or a value with a stray trailing space all pass a presence check and
# then fail at the first call that builds a URL out of them — which is
# in production, on a real booking, long after the deploy went green.
# The sibling report on the bot side is DRF-1221 (AYLA_BASE_URL).
#
# So: every setting whose value is *by contract* an absolute http(s) URL
# gets its form checked at boot. Structural only — scheme, host, port,
# no whitespace. No DNS, no TCP: startup must not depend on the network.
#
# Membership rule — a variable belongs here iff its value is handed to
# urljoin / an HTTP client as a whole URL:
#
# - AYLA_INTERNAL_BASE_URL, AYLA_PUBLIC_BASE_URL: the two bases of
#   core.ayla_urls.AylaUrlBuilder. Today the builder only checks them
#   for emptiness (`if not self.internal_base: raise RuntimeError`) —
#   at call time, i.e. exactly the late failure this gate removes. The
#   public base also becomes the YooKassa `return_url`, where a
#   malformed value is a payment the customer cannot come back from.
# - BOT_PLATFORM_BASE_URL: appointments.infrastructure.outbox.publisher
#   posts base + BOT_PLATFORM_INGEST_PATH. A malformed base makes every
#   cross-service event delivery fail inside a beat task that logs and
#   swallows — the quietest possible failure mode.
# - YCLIENTS_API_BASE_URL: base for every YClients catalog call
#   (services.integrations.yclients.client). Has a working default, so
#   only an explicit override can break it — and an override is exactly
#   when a typo happens.
# - MINIO_ENDPOINT: `endpoint_url` for the S3 storage backend below. A
#   malformed value takes every media upload and every signed URL with
#   it, at first use rather than at boot.
# - OPENAI_BASE_URL: `base_url` of the OpenAI client
#   (ai.services.llm_client). Optional by design; when set it must be a
#   URL.
#
# Deliberately NOT here:
# - YOOKASSA_WEBHOOK_ALLOWED_IPS (list of IPs/CIDRs), AYLA_INTERNAL_API_TOKEN,
#   GOOGLE_CLIENT_ID, APPLE_CLIENT_ID — not URLs. Checking them as URLs
#   would be the same defect pointing the other way.
# - REDIS_URL — a URL, but `redis://` / `rediss://`; the http-only scheme
#   set would reject every valid value.
# - OPENAI_PROXY — legitimately `socks5://` as well as `http://`.
# - SENTRY_DSN — a credential-bearing URL whose only consumer degrades to
#   a no-op. Making telemetry config abort a boot trades an observability
#   gap for an outage; that call belongs to the owner, not to this gate.
# - CORS_ALLOWED_ORIGINS / DJANGO_ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS —
#   comma-separated lists of origins/hosts, not single URLs (and
#   django-cors-headers already system-checks the first one).
#
# Absence is NOT an error here, and neither is an explicitly blank value
# (`NAME=` in .env): for these settings empty is the documented
# off-switch — the outbox publisher no-ops, OPENAI_BASE_URL falls back to
# the OpenAI default, AylaUrlBuilder raises only when a caller needs the
# base. None of them are in _REQUIRED_PROD_ENV, and adding a presence
# requirement through this call would widen what a deploy must provide
# and could stop a currently-working environment from booting. Whether
# any of them should also be *mandatory* in production is a separate
# decision that belongs in _REQUIRED_PROD_ENV above.
#
# Strictness is the same DJANGO_ENV gate as above — the pilot deploys
# with DJANGO_ENV=staging (.github/workflows/ci.yml) and therefore gets a
# warning, never a failed boot.
_URL_SHAPED_ENV = (
    "AYLA_INTERNAL_BASE_URL",
    "AYLA_PUBLIC_BASE_URL",
    "BOT_PLATFORM_BASE_URL",
    "YCLIENTS_API_BASE_URL",
    "MINIO_ENDPOINT",
    "OPENAI_BASE_URL",
)
enforce_url_env(
    _URL_SHAPED_ENV,
    "Each value is used as a whole URL (urljoin / HTTP client base), so a "
    "malformed one fails at first use in production rather than at boot.",
)

# CORS
INSTALLED_APPS += ['corsheaders']
MIDDLEWARE = ['corsheaders.middleware.CorsMiddleware'] + MIDDLEWARE

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get('CORS_ALLOWED_ORIGINS', '').split(',')
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

# Database - PostgreSQL
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'specialist_marketplace'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
        'HOST': os.environ.get('POSTGRES_HOST', 'db'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
    }
}

# Security headers (behind nginx with SSL)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = False  # nginx handles SSL termination
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = [
    f'https://{host.strip()}'
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',')
    if host.strip()
]

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Static / Media
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# S3-compatible storage (Minio / AWS S3)
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "access_key": os.environ.get("MINIO_ROOT_USER", "minioadmin"),
            "secret_key": os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"),
            "bucket_name": os.environ.get("S3_BUCKET_NAME", "beautygo-media"),
            "endpoint_url": os.environ.get("MINIO_ENDPOINT", "http://minio:9000"),
            "custom_domain": None,
            "default_acl": "public-read",
            "file_overwrite": False,
        },
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Logging — switch the LOGGING dict from base.py over to JSON output.
# Everything else (filters, loggers, handlers, levels) is inherited.
LOGGING["handlers"]["console"]["formatter"] = "json"
