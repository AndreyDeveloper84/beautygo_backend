"""URL conf for ``/api/v1/internal/tenants/`` — DRF-1525.

Mounted in ``djangoProject/urls.py`` under the internal prefix. Two
routes: the provisioning-only «салон по slug» call the bot's
«подключить салон» screen makes. See ``tenants/internal_api.py``.
"""

from django.urls import path

from users.internal_salon_admin_api import InternalSalonAdminLinkView

from .internal_api import InternalEnsureTenantView, InternalSoloWorkspaceView

urlpatterns = [
    path("", InternalEnsureTenantView.as_view(), name="internal-tenants-ensure"),
    # DRF-1828 (G1/G4): solo-workspace — Tenant с UUID бота + DRAFT-профиль.
    path(
        "solo-workspaces/",
        InternalSoloWorkspaceView.as_view(),
        name="internal-solo-workspace-provision",
    ),
    # DRF-2085 (OWNER RULING 18.09, вариант А): свежий администратор
    # салона + TUR + связь с MAX-личностью — под СВОИМ credential
    # (IsSalonAdminLinkBearer), не под provisioning-токеном соседей выше.
    path(
        "<slug:slug>/salon-admins/",
        InternalSalonAdminLinkView.as_view(),
        name="internal-salon-admin-link",
    ),
]
