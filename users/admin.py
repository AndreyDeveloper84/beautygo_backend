from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import OTPCode, Profile, Service, User


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = 'Профиль'
    fk_name = 'user'


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline]
    list_display = ('username', 'email', 'role', 'phone', 'is_verified', 'is_staff')
    list_filter = ('role', 'is_staff', 'is_active')
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Дополнительно', {'fields': ('role', 'phone', 'is_verified')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Дополнительно', {'fields': ('role', 'phone')}),
    )


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = ('phone', 'code', 'created_at', 'expires_at', 'is_used', 'attempts')
    list_filter = ('is_used',)
    search_fields = ('phone',)
    readonly_fields = ('phone', 'code', 'created_at', 'expires_at')


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'specialist', 'price', 'duration_minutes', 'created_at')
    list_filter = ('specialist', 'created_at')
    search_fields = ('name', 'description', 'specialist__username')
    readonly_fields = ('created_at',)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'full_name', 'city', 'experience_years',
        'default_location_lat', 'default_location_lng',
    )
    list_filter = ('city',)
    search_fields = ('full_name', 'user__username', 'city')
