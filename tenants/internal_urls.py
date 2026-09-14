"""URL conf for ``/api/v1/internal/tenants/`` — DRF-1525.

Mounted in ``djangoProject/urls.py`` under the internal prefix. Two
routes: the provisioning-only «салон по slug» call the bot's
«подключить салон» screen makes. See ``tenants/internal_api.py``.
"""

from django.urls import path

from .internal_api import InternalEnsureTenantView, InternalSoloWorkspaceView

urlpatterns = [
    path("", InternalEnsureTenantView.as_view(), name="internal-tenants-ensure"),
    # DRF-1828 (G1/G4): solo-workspace — Tenant с UUID бота + DRAFT-профиль.
    path(
        "solo-workspaces/",
        InternalSoloWorkspaceView.as_view(),
        name="internal-solo-workspace-provision",
    ),
]
