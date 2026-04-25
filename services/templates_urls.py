"""URL config for Service Templates API (DRF-198)."""
from django.urls import path

from .templates_views import ServiceTemplatesListView, SupportedRegionsView

urlpatterns = [
    # /regions/ must precede the list view to avoid shadowing.
    path('regions/', SupportedRegionsView.as_view(), name='service-templates-regions'),
    path('', ServiceTemplatesListView.as_view(), name='service-templates-list'),
]
