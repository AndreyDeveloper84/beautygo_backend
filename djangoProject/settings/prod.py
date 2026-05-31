import os

from .base import *

from core.env_strictness import enforce_required_env

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
