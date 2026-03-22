from rest_framework.routers import DefaultRouter

from .views import ServiceCategoryViewSet, ServiceViewSet

router = DefaultRouter()
router.register(r'categories', ServiceCategoryViewSet, basename='categories')
router.register(r'', ServiceViewSet, basename='services')

urlpatterns = router.urls
