"""URLs for the internal Bearer services catalog (#1016 S2).

Mounted at /api/v1/internal/services/ in djangoProject/urls.py.
``categories`` is registered before the catch-all so the router
resolves /internal/services/categories/ to the category list rather
than treating "categories" as a service detail lookup.
"""
from rest_framework.routers import DefaultRouter

from .internal_api import InternalServiceCategoryViewSet, InternalServiceViewSet

router = DefaultRouter()
router.register(
    r'categories', InternalServiceCategoryViewSet,
    basename='internal-categories',
)
router.register(r'', InternalServiceViewSet, basename='internal-services')

urlpatterns = router.urls
