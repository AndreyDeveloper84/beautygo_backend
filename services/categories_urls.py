from rest_framework.routers import DefaultRouter

from .views import ServiceCategoryViewSet

router = DefaultRouter()
router.register(r'', ServiceCategoryViewSet, basename='categories-api')

urlpatterns = router.urls
