"""URLs for the internal Bearer specialist catalog (#1016 S2).

Mounted at /api/v1/internal/specialists/ in djangoProject/urls.py.
"""
from rest_framework.routers import DefaultRouter

from .internal_catalog_api import InternalSpecialistViewSet

router = DefaultRouter()
router.register(r'', InternalSpecialistViewSet, basename='internal-specialists')

urlpatterns = router.urls
