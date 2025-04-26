from .base import *

DEBUG = True

INSTALLED_APPS += ['corsheaders']
MIDDLEWARE = ['corsheaders.middleware.CorsMiddleware'] + MIDDLEWARE

CORS_ALLOWED_ORIGINS = [
    "http://localhost:8000",
]

ALLOWED_HOSTS = ["*"]
