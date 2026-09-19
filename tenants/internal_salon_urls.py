"""URL conf for ``/api/v1/internal/salons/`` — DRF-2117.

Mounted in ``djangoProject/urls.py`` under the internal prefix. The salon
is named by ``slug`` in the URL and proved by the actor's ``admin`` TUR
(``IsInternalBearerForSalonSubject``); see ``tenants/internal_salon_api.py``.
"""

from django.urls import path

from .internal_salon_api import InternalSalonReadinessView

urlpatterns = [
    path(
        "<slug:slug>/readiness/",
        InternalSalonReadinessView.as_view(),
        name="internal-salon-readiness",
    ),
]
