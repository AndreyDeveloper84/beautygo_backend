from __future__ import annotations

from typing import Any

from rest_framework import serializers

from .goal_resolution import build_category_goal_index
from .models import (
    SalonService,
    Service,
    ServiceCategory,
    SpecialistService,
)

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


# --- S3A internal catalog serializers (Bearer mirror, #1044 / #200) ---


class SalonServiceInternalSerializer(serializers.ModelSerializer):
    """Read mirror of the salon-offer layer for the bot (S3B).

    ``goals`` (DRF-1308) — цели, уже разрешённые по дереву категорий на
    стороне Ayla. У бота нет таблицы категорий вообще: он хранит UUID
    категории в ``raw`` и разрешить его не может, поэтому обход дерева
    делается здесь, а в зеркало едет готовый список. ADR-0009: зеркало —
    read-replica, а не источник истины.

    Список может быть пустым — это честное «цель не заявлена», а не сбой.
    Ложную цель ради покрытия не подставляем (решение владельца, п. 4).
    """

    goals = serializers.SerializerMethodField()

    class Meta:
        model = SalonService
        fields = [
            'id', 'tenant', 'template', 'category', 'name',
            'duration_minutes', 'base_price', 'requires_health_check',
            'is_active', 'source', 'goals', 'created_at', 'updated_at',
        ]

    def get_goals(self, obj: SalonService) -> list[dict[str, str]]:
        """`[{"key": ..., "label": ...}, …]`, порядок — sort_order цели.

        Индекс кладёт вьюсет в контекст один раз на запрос; сборка его
        на лету здесь — запасной путь для вызова сериализатора вне
        вьюсета (тесты, management-команды), а не рабочий режим.
        """
        index = self.context.get('category_goal_index')
        if index is None:
            index = build_category_goal_index()
            self.context['category_goal_index'] = index
        return index.goals_for_service(obj)


class SpecialistServiceInternalSerializer(serializers.ModelSerializer):
    """Read mirror of the BOOKABLE layer — the bot's stable booking key.

    Exposes the resolved duration / health-check (cascade specialist ->
    salon -> template, D1 escalate-only) so the bot never re-derives them,
    plus the YClients staff id for cross-source reconciliation.

    Catalog-link contract (C6, orchestrator decision 2026-07-19): the bot
    links its catalog rows to Ayla by ``(category_slug, normalized name)``
    with duration as tiebreaker — ServiceTemplate intentionally has NO
    slug field (no migration for matching's sake in the pilot).
    Normalization rules (fixed for both sides): lower, trim, ё→е,
    collapse whitespace, strip «» quotes. This serializer therefore
    exposes raw ``name`` + ``category_slug`` alongside ``template``;
    normalization itself happens on the bot side (W3).
    """

    resolved_duration = serializers.SerializerMethodField()
    resolved_requires_health_check = serializers.SerializerMethodField()
    template = serializers.SerializerMethodField()
    # C6 link keys (additive): raw values the bot normalizes per the
    # contract above.
    name = serializers.CharField(source='salon_service.name', read_only=True)
    category_slug = serializers.CharField(
        source='salon_service.category.slug', read_only=True,
    )
    yclients_staff_id = serializers.CharField(
        source='specialist.yclients_staff_id', default='', read_only=True,
    )
    # Canonical Ayla User.id (NOT SpecialistProfile.id) — the bot maps this to
    # CatalogMaster.ayla_user_id. reviews_count/rating let the bot populate
    # CatalogMaster.review_count/rating in the same catalog sync (#1052/#1060),
    # no second call to /internal/specialists/.
    user_id = serializers.UUIDField(source='specialist.user_id', read_only=True)
    reviews_count = serializers.IntegerField(
        source='specialist.reviews_count', read_only=True,
    )
    rating = serializers.DecimalField(
        source='specialist.rating', max_digits=2, decimal_places=1,
        read_only=True,
    )

    class Meta:
        model = SpecialistService
        fields = [
            'id', 'salon_service', 'specialist', 'user_id', 'tenant',
            'template', 'name', 'category_slug',
            'duration_minutes', 'resolved_duration',
            'requires_health_check', 'resolved_requires_health_check',
            'price', 'buffer_after_minutes', 'is_active',
            'yclients_staff_id', 'reviews_count', 'rating',
            'created_at', 'updated_at',
        ]

    def get_resolved_duration(self, obj: SpecialistService) -> int | None:
        return obj.resolved_duration()

    def get_resolved_requires_health_check(self, obj: SpecialistService) -> bool:
        return obj.resolved_requires_health_check()

    def get_template(self, obj: SpecialistService) -> str | None:
        template_id = obj.salon_service.template_id
        return str(template_id) if template_id is not None else None
