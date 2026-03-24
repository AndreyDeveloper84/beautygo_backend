"""Django Admin configuration for users app."""
from __future__ import annotations

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

from .models import (
    DeviceToken, OTPCode, Profile, SocialAccount, SpecialistProfile, User,
)


# ─── Actions ─────────────────────────────────────────────────────────────────

@admin.action(description='Заблокировать выбранных пользователей')
def block_users(modeladmin, request, queryset):
    updated = queryset.exclude(is_superuser=True).update(is_active=False)
    modeladmin.message_user(
        request, f'Заблокировано пользователей: {updated}', messages.SUCCESS,
    )


@admin.action(description='Разблокировать выбранных пользователей')
def unblock_users(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    modeladmin.message_user(
        request, f'Разблокировано пользователей: {updated}', messages.SUCCESS,
    )


@admin.action(description='✅ Подтвердить мастеров (→ active)')
def approve_specialists(modeladmin, request, queryset):
    updated = queryset.filter(
        status__in=[
            SpecialistProfile.ProfileStatus.PENDING,
            SpecialistProfile.ProfileStatus.DRAFT,
        ],
    ).update(status=SpecialistProfile.ProfileStatus.ACTIVE)
    modeladmin.message_user(
        request, f'Подтверждено мастеров: {updated}', messages.SUCCESS,
    )


@admin.action(description='❌ Отклонить мастеров (→ draft)')
def reject_specialists(modeladmin, request, queryset):
    updated = queryset.exclude(
        status=SpecialistProfile.ProfileStatus.DRAFT,
    ).update(status=SpecialistProfile.ProfileStatus.DRAFT)
    modeladmin.message_user(
        request, f'Отклонено мастеров: {updated}', messages.WARNING,
    )


# ─── Inlines ─────────────────────────────────────────────────────────────────

class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = 'Профиль клиента'
    fk_name = 'user'
    fields = ('full_name', 'city', 'avatar', 'bio')


# ─── UserAdmin ───────────────────────────────────────────────────────────────

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline]
    actions = [block_users, unblock_users]

    list_display = (
        'phone', 'get_full_name_display', 'role',
        'is_active', 'is_verified', 'date_joined',
    )
    list_filter = ('role', 'is_active', 'is_verified', 'is_staff')
    search_fields = ('phone', 'email', 'first_name', 'last_name', 'username')
    ordering = ('-date_joined',)
    readonly_fields = ('date_joined', 'last_login')

    fieldsets = BaseUserAdmin.fieldsets + (
        ('BeautyGO', {'fields': ('role', 'phone', 'is_verified', 'deleted_at')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('BeautyGO', {'fields': ('role', 'phone')}),
    )

    @admin.display(description='Имя')
    def get_full_name_display(self, obj: User) -> str:
        name = obj.get_full_name()
        return name if name.strip() else '—'


# ─── OTPCodeAdmin ─────────────────────────────────────────────────────────────

@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = ('phone', 'code', 'created_at', 'expires_at', 'is_used', 'attempts')
    list_filter = ('is_used',)
    search_fields = ('phone',)
    readonly_fields = ('phone', 'code', 'created_at', 'expires_at')
    ordering = ('-created_at',)


# ─── ProfileAdmin ─────────────────────────────────────────────────────────────

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'full_name', 'city', 'experience_years')
    list_filter = ('city',)
    search_fields = ('full_name', 'user__phone', 'user__username', 'city')
    raw_id_fields = ('user',)


# ─── SpecialistProfileAdmin ───────────────────────────────────────────────────

@admin.register(SpecialistProfile)
class SpecialistProfileAdmin(admin.ModelAdmin):
    actions = [approve_specialists, reject_specialists]

    list_display = (
        'display_name', 'get_phone', 'status_badge',
        'rating', 'reviews_count', 'is_available', 'created_at',
    )
    list_filter = ('status', 'is_available')
    search_fields = ('display_name', 'user__phone', 'user__username', 'address')
    readonly_fields = ('rating', 'reviews_count', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('user',)

    fieldsets = (
        ('Основное', {
            'fields': ('user', 'display_name', 'avatar', 'bio', 'status', 'is_available'),
        }),
        ('Опыт и локация', {
            'fields': ('experience_years', 'address', 'location_lat', 'location_lng'),
        }),
        ('Статистика', {
            'fields': ('rating', 'reviews_count'),
            'classes': ('collapse',),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Телефон')
    def get_phone(self, obj: SpecialistProfile) -> str:
        return obj.user.phone or '—'

    @admin.display(description='Статус')
    def status_badge(self, obj: SpecialistProfile) -> str:
        colors = {
            'draft': '#888',
            'pending': '#f59e0b',
            'active': '#22c55e',
        }
        color = colors.get(obj.status, '#888')
        label = obj.get_status_display()
        return format_html(
            '<span style="color:{};font-weight:bold">● {}</span>',
            color, label,
        )


# ─── DeviceTokenAdmin ─────────────────────────────────────────────────────────

@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ('user', 'app_type', 'platform', 'is_active', 'created_at')
    list_filter = ('app_type', 'platform', 'is_active')
    search_fields = ('user__phone', 'user__username', 'token')
    readonly_fields = ('created_at',)
    raw_id_fields = ('user',)


# ─── SocialAccountAdmin ───────────────────────────────────────────────────────

@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ('user', 'provider', 'provider_uid', 'created_at')
    list_filter = ('provider',)
    search_fields = ('user__phone', 'user__username', 'provider_uid')
    readonly_fields = ('created_at', 'extra_data')
    raw_id_fields = ('user',)
