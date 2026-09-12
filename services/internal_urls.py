"""URLs for the internal Bearer services catalog (#1016 S2).

Mounted at /api/v1/internal/services/ in djangoProject/urls.py.
``categories`` and ``directions`` are registered before the catch-all so
the router resolves /internal/services/categories/ (/directions/) to the
list rather than treating the word as a service detail lookup;
``templates/`` (DRF-1798) is a plain path listed before the router for the
same reason.
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from .internal_api import (
    InternalServiceCategoryViewSet,
    InternalServiceDirectionViewSet,
    InternalServiceViewSet,
)
from .templates_views import InternalServiceTemplatesListView

router = DefaultRouter()
router.register(
    r'categories', InternalServiceCategoryViewSet,
    basename='internal-categories',
)
router.register(
    r'directions', InternalServiceDirectionViewSet,
    basename='internal-directions',
)
router.register(r'', InternalServiceViewSet, basename='internal-services')

urlpatterns = [
    path(
        'templates/', InternalServiceTemplatesListView.as_view(),
        name='internal-service-templates-list',
    ),
    *router.urls,
]
