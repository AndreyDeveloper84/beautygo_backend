"""URL conf for ``/api/v1/internal/tenants/`` — DRF-1525.

Mounted in ``djangoProject/urls.py`` under the internal prefix. One
route: the provisioning-only «салон по slug» call the bot's
«подключить салон» screen makes. See ``tenants/internal_api.py``.
"""

from django.urls import path

from .internal_api import InternalEnsureTenantView

urlpatterns = [
    path("", InternalEnsureTenantView.as_view(), name="internal-tenants-ensure"),
]
