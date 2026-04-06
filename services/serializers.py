from __future__ import annotations

from typing import Any

from rest_framework import serializers

from .models import Service, ServiceCategory

# --- Public serializers (Client App) ---


class SpecialistShortSerializer(serializers.Serializer):
    """Minimal specialist info for service listings."""
    id = serializers.UUIDField(source='specialist.id')
    display_name = serializers.CharField(
        source='specialist.display_name',
        default='',
    )
    avatar = serializers.ImageField(
        source='specialist.avatar',
        default=None,
    )
    rating = serializers.DecimalField(
        source='specialist.rating',
        max_digits=2, decimal_places=1, default=0.0,
    )
    reviews_count = serializers.IntegerField(
        source='specialist.reviews_count',
        default=0,
    )


class ServicePublicListSerializer(serializers.ModelSerializer):
    """Public service listing for Client App."""
    category_name = serializers.CharField(
        source='category.name', read_only=True, default=None,
    )
    specialist_info = SpecialistShortSerializer(source='*', read_only=True)

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'price', 'duration_minutes',
            'category', 'category_name', 'image',
            'specialist_info', 'created_at',
        ]


class ServicePublicDetailSerializer(ServicePublicListSerializer):
    """Detailed service view — includes specialist address."""
    specialist_address = serializers.CharField(
        source='specialist.address',
        default='',
    )
    specialist_location_lat = serializers.DecimalField(
        source='specialist.location_lat',
        max_digits=9, decimal_places=6, default=None,
    )
    specialist_location_lng = serializers.DecimalField(
        source='specialist.location_lng',
        max_digits=9, decimal_places=6, default=None,
    )

    class Meta(ServicePublicListSerializer.Meta):
        fields = ServicePublicListSerializer.Meta.fields + [
            'specialist_address',
            'specialist_location_lat',
            'specialist_location_lng',
        ]


class ServiceCategorySerializer(serializers.ModelSerializer):
    """Category with nested children and specialists count."""
    children = serializers.SerializerMethodField()
    specialists_count = serializers.SerializerMethodField()

    class Meta:
        model = ServiceCategory
        fields = [
            'id', 'name', 'slug', 'icon', 'sort_order',
            'specialists_count', 'children',
        ]

    def get_children(self, obj: ServiceCategory) -> list:
        """Children categories (uses prefetched data, depth-limited)."""
        depth = self.context.get('depth', 0)
        if depth >= 2:
            return []
        children = obj.children.all()  # uses prefetch cache
        context = {**self.context, 'depth': depth + 1}
        return ServiceCategorySerializer(
            children, many=True, context=context,
        ).data

    def get_specialists_count(self, obj: ServiceCategory) -> int:
        """Distinct specialists count (uses annotation, no extra query)."""
        return getattr(obj, 'specialists_count', 0)


ALLOWED_IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/webp')
MAX_IMAGE_SIZE = 5 * 1024 * 1024


class ServiceSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(
        source='category.name', read_only=True, default=None,
    )
    is_active = serializers.BooleanField(default=True)

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'price', 'duration_minutes',
            'category', 'category_name', 'image', 'is_active',
            'sort_order', 'buffer_after_minutes',
            'specialist', 'created_at', 'updated_at',
        ]
        read_only_fields = ['specialist', 'created_at', 'updated_at']

    def validate_price(self, value: Any) -> Any:
        if value < 1:
            raise serializers.ValidationError(
                "Цена должна быть не менее 1 рубля"
            )
        return value

    def validate_duration_minutes(self, value: Any) -> Any:
        if value < 15:
            raise serializers.ValidationError(
                "Длительность должна быть не менее 15 минут"
            )
        if value > 480:
            raise serializers.ValidationError(
                "Длительность не может превышать 8 часов (480 минут)"
            )
        return value

    def validate_buffer_after_minutes(self, value: Any) -> Any:
        if value < 0 or value > 120:
            raise serializers.ValidationError(
                "Буфер должен быть от 0 до 120 минут"
            )
        return value

    def validate_image(self, value: Any) -> Any:
        """Validate image content type and file size."""
        if value.content_type not in ALLOWED_IMAGE_TYPES:
            raise serializers.ValidationError(
                "Допустимые форматы: JPEG, PNG, WebP"
            )
        if value.size > MAX_IMAGE_SIZE:
            raise serializers.ValidationError(
                "Размер файла не должен превышать 5 МБ"
            )
        return value
