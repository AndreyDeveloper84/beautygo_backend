import os

from .base import *

DEBUG = True

# Default to eager Celery in dev — synchronous task execution keeps
# `runserver` self-contained (no separate worker process). Override
# CELERY_TASK_ALWAYS_EAGER=False in env when testing real async flow.
if "CELERY_TASK_ALWAYS_EAGER" not in os.environ:
    CELERY_TASK_ALWAYS_EAGER = True

INSTALLED_APPS += ['corsheaders']
MIDDLEWARE = ['corsheaders.middleware.CorsMiddleware'] + MIDDLEWARE

CORS_ALLOWED_ORIGINS = [
    "http://localhost:8082",
    "http://194.87.99.126:8000",
]

ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True  # если нужны куки

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# S3-compatible storage (Minio local)
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "access_key": os.environ.get("MINIO_ROOT_USER", "minioadmin"),
            "secret_key": os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"),
            "bucket_name": "beautygo-media",
            "endpoint_url": os.environ.get("MINIO_ENDPOINT", "http://localhost:9000"),
            "custom_domain": None,
            "default_acl": "public-read",
            "file_overwrite": False,
        },
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
