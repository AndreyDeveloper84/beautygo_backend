from rest_framework.routers import DefaultRouter

from .specialists_api import SpecialistViewSet

router = DefaultRouter()
router.register(r'', SpecialistViewSet, basename='specialists')

urlpatterns = router.urls
