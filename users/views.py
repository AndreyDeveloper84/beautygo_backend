from datetime import datetime, timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, viewsets, permissions
from rest_framework.decorators import action
from django_filters.rest_framework import FilterSet, filters

from .serializers import (
    RegisterSerializer, ServiceSerializer, CategorySerializer,
    SpecialistProfileSerializer, ScheduleSerializer,
    BookingSerializer, ReviewSerializer,
)
from .models import Service, Category, SpecialistProfile, Schedule, Booking, Review


# ──────────────── Permissions ────────────────

class IsSpecialist(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == 'specialist'


class IsClient(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == 'client'


# ──────────────── Auth ────────────────

class RegisterView(APIView):
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({"message": "Регистрация прошла успешно"}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ──────────────── Categories ────────────────

class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]


# ──────────────── Specialist Profiles ────────────────

class SpecialistProfileFilter(FilterSet):
    city = filters.CharFilter(field_name='city', lookup_expr='icontains')
    specialization = filters.CharFilter(field_name='specialization', lookup_expr='icontains')
    category = filters.NumberFilter(field_name='categories', lookup_expr='exact')

    class Meta:
        model = SpecialistProfile
        fields = ['city', 'specialization', 'category', 'is_available']


class SpecialistProfileViewSet(viewsets.ModelViewSet):
    serializer_class = SpecialistProfileSerializer
    filterset_class = SpecialistProfileFilter

    def get_queryset(self):
        return SpecialistProfile.objects.select_related('user').all()

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated(), IsSpecialist()]

    def perform_update(self, serializer):
        if serializer.instance.user != self.request.user:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Вы можете редактировать только свой профиль.")
        serializer.save()


# ──────────────── Services ────────────────

class ServiceFilter(FilterSet):
    min_price = filters.NumberFilter(field_name='price', lookup_expr='gte')
    max_price = filters.NumberFilter(field_name='price', lookup_expr='lte')
    name = filters.CharFilter(field_name='name', lookup_expr='icontains')
    category = filters.NumberFilter(field_name='category')
    specialist = filters.NumberFilter(field_name='specialist')

    class Meta:
        model = Service
        fields = ['name', 'min_price', 'max_price', 'duration_minutes', 'specialist', 'category']


class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer
    filterset_class = ServiceFilter

    def get_queryset(self):
        if self.request.user.is_authenticated and self.request.user.role == 'specialist':
            if self.action in ('update', 'partial_update', 'destroy'):
                return Service.objects.filter(specialist=self.request.user)
        return Service.objects.filter(is_active=True)

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated(), IsSpecialist()]

    def perform_create(self, serializer):
        serializer.save(specialist=self.request.user)


# ──────────────── Schedule ────────────────

class ScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleSerializer

    def get_queryset(self):
        specialist_id = self.request.query_params.get('specialist')
        if specialist_id:
            return Schedule.objects.filter(specialist_id=specialist_id, is_active=True)
        if self.request.user.is_authenticated and self.request.user.role == 'specialist':
            return Schedule.objects.filter(specialist=self.request.user)
        return Schedule.objects.filter(is_active=True)

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated(), IsSpecialist()]

    def perform_create(self, serializer):
        serializer.save(specialist=self.request.user)


# ──────────────── Bookings ────────────────

class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        user = self.request.user
        if user.role == 'client':
            return Booking.objects.filter(client=user)
        elif user.role == 'specialist':
            return Booking.objects.filter(specialist=user)
        return Booking.objects.none()

    def get_permissions(self):
        return [permissions.IsAuthenticated()]

    def perform_create(self, serializer):
        service = serializer.validated_data['service']
        start_time = serializer.validated_data['start_time']
        duration = timedelta(minutes=service.duration_minutes)
        start_dt = datetime.combine(datetime.today(), start_time)
        end_time = (start_dt + duration).time()

        serializer.save(
            client=self.request.user,
            specialist=service.specialist,
            end_time=end_time,
            status='pending',
        )

    @action(detail=True, methods=['patch'])
    def confirm(self, request, pk=None):
        booking = self.get_object()
        if booking.specialist != request.user:
            return Response({"error": "Только специалист может подтвердить запись."}, status=status.HTTP_403_FORBIDDEN)
        booking.status = 'confirmed'
        booking.save()
        return Response(BookingSerializer(booking).data)

    @action(detail=True, methods=['patch'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        if booking.client != request.user and booking.specialist != request.user:
            return Response({"error": "Нет прав для отмены."}, status=status.HTTP_403_FORBIDDEN)
        booking.status = 'cancelled'
        booking.save()
        return Response(BookingSerializer(booking).data)

    @action(detail=True, methods=['patch'])
    def complete(self, request, pk=None):
        booking = self.get_object()
        if booking.specialist != request.user:
            return Response({"error": "Только специалист может завершить запись."}, status=status.HTTP_403_FORBIDDEN)
        booking.status = 'completed'
        booking.save()
        return Response(BookingSerializer(booking).data)


# ──────────────── Reviews ────────────────

class ReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ReviewSerializer
    http_method_names = ['get', 'post', 'delete']

    def get_queryset(self):
        specialist_id = self.request.query_params.get('specialist')
        if specialist_id:
            return Review.objects.filter(specialist_id=specialist_id)
        if self.request.user.is_authenticated:
            if self.request.user.role == 'client':
                return Review.objects.filter(client=self.request.user)
            elif self.request.user.role == 'specialist':
                return Review.objects.filter(specialist=self.request.user)
        return Review.objects.none()

    def get_permissions(self):
        if self.action == 'list':
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def perform_create(self, serializer):
        booking = serializer.validated_data['booking']
        if booking.client != self.request.user:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Оставить отзыв может только клиент этой записи.")
        if booking.status != 'completed':
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Отзыв можно оставить только после завершённой записи.")
        serializer.save(
            client=self.request.user,
            specialist=booking.specialist,
        )
