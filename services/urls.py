from rest_framework.routers import DefaultRouter

from .views import ServiceCategoryViewSet, ServicePublicViewSet, ServiceViewSet

router = DefaultRouter()
router.register(r'categories', ServiceCategoryViewSet, basename='categories')
router.register(r'search', ServicePublicViewSet, basename='services-public')
router.register(r'', ServiceViewSet, basename='services')

urlpatterns = router.urls
