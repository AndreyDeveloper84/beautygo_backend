"""URLs for the S3A internal Bearer canonical-catalog mirror (#1044 / #200).

Mounted at /api/v1/internal/catalog/ in djangoProject/urls.py. Read-only
mirror of the new SalonService / SpecialistService layer the Ayla bot (S3B)
consumes. The legacy /api/v1/internal/services/ Service mirror is unchanged.
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from .internal_api import (
    InternalSalonServiceViewSet,
    InternalSpecialistServiceViewSet,
)
from .webhooks import YClientsBusyWebhookView

router = DefaultRouter()
router.register(
    r'salon-services', InternalSalonServiceViewSet,
    basename='internal-salon-services',
)
router.register(
    r'specialist-services', InternalSpecialistServiceViewSet,
    basename='internal-specialist-services',
)

urlpatterns = router.urls + [
    # S3-CAL.3 inbound YClients busy webhook (not a viewset — signature-authed).
    path(
        'yclients/busy-webhook/',
        YClientsBusyWebhookView.as_view(),
        name='yclients-busy-webhook',
    ),
]
