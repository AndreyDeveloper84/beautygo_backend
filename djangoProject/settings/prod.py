import os

from django.core.exceptions import ImproperlyConfigured

from .base import *

# Security
DEBUG = False
# Allow debug OTP code on dev server (never in real production)
OTP_DEBUG_CODE_ENABLED = os.environ.get('OTP_DEBUG_CODE_ENABLED', '').lower() == 'true'
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',')


# Fail-fast on OAuth audience bindings.
#
# Without these, social_auth.verify_google_token and verify_apple_token
# silently skip the "aud" claim check — any id-token issued by Google/Apple
# for ANY application would be accepted. That is the classic confused-deputy
# attack that hands out accounts in the wrong app to whoever signed into
# some other OAuth client.
#
# dev.py intentionally tolerates missing values so local work does not need
# real OAuth credentials; prod must not.
_REQUIRED_OAUTH_ENV = ("GOOGLE_CLIENT_ID", "APPLE_CLIENT_ID")
_missing_oauth = [name for name in _REQUIRED_OAUTH_ENV if not os.environ.get(name)]
if _missing_oauth:
    raise ImproperlyConfigured(
        "Production requires OAuth audience bindings: "
        f"missing {', '.join(_missing_oauth)}. "
        "Without these, Google/Apple id-tokens bypass audience validation, "
        "enabling cross-application account takeover."
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

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
