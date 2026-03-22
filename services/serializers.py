from rest_framework import serializers

from .models import Service, ServiceCategory


class ServiceCategorySerializer(serializers.ModelSerializer):
    """Category with nested children."""
    children = serializers.SerializerMethodField()

    class Meta:
        model = ServiceCategory
        fields = ['id', 'name', 'slug', 'icon', 'sort_order', 'children']

    def get_children(self, obj):
        children = obj.children.filter(is_active=True)
        return ServiceCategorySerializer(children, many=True).data


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
            'sort_order', 'specialist', 'created_at',
        ]
        read_only_fields = ['specialist', 'created_at']

    def validate_image(self, value):
        if value.content_type not in ALLOWED_IMAGE_TYPES:
            raise serializers.ValidationError(
                "Допустимые форматы: JPEG, PNG, WebP"
            )
        if value.size > MAX_IMAGE_SIZE:
            raise serializers.ValidationError(
                "Размер файла не должен превышать 5 МБ"
            )
        return value
