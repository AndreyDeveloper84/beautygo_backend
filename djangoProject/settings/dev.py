from .base import *

DEBUG = True

INSTALLED_APPS += ['corsheaders']
MIDDLEWARE = ['corsheaders.middleware.CorsMiddleware'] + MIDDLEWARE

CORS_ALLOWED_ORIGINS = [
    "http://localhost:8082",
    "http://194.87.99.126:8000",
]

ALLOWED_HOSTS = ["*"]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}