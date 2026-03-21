from django_filters.rest_framework import FilterSet, filters
from rest_framework import permissions, viewsets

from users.permissions import IsProApp, IsSpecialist

from .models import Service
from .serializers import ServiceSerializer


class ServiceViewSet(viewsets.ModelViewSet):
    """🟣 Pro only — CRUD услуг мастера."""
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticated, IsProApp, IsSpecialist]

    def get_queryset(self):
        return Service.objects.filter(specialist=self.request.user)

    def perform_create(self, serializer):
        serializer.save(specialist=self.request.user)


class ServiceFilter(FilterSet):
    min_price = filters.NumberFilter(field_name="price", lookup_expr='gte')
    max_price = filters.NumberFilter(field_name="price", lookup_expr='lte')
    name = filters.CharFilter(field_name="name", lookup_expr='icontains')

    class Meta:
        model = Service
        fields = [
            'name', 'min_price', 'max_price',
            'duration_minutes', 'specialist',
        ]
