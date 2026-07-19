"""W2-local ROOT_URLCONF — TEMPORARY shim.

Mounts billing internal urls the same way W1's B-5 patch will mount
them in djangoProject/urls.py. Delete together with settings_w2.py
once the canonical settings/urlconf include the billing app.
"""
from django.urls import include, path

from djangoProject.urls import urlpatterns as base_urlpatterns


urlpatterns = base_urlpatterns + [
    path("api/v1/internal/billing/", include("billing.internal_urls")),
]
