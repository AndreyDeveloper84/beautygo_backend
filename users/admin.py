from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Category, SpecialistProfile, Service, Schedule, Booking, Review


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'role', 'phone', 'is_active')
    list_filter = ('role', 'is_active')
    fieldsets = UserAdmin.fieldsets + (
        ('BeautyGo', {'fields': ('role', 'phone')}),
    )


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'icon')
    search_fields = ('name',)


@admin.register(SpecialistProfile)
class SpecialistProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'specialization', 'city', 'experience_years', 'is_available')
    list_filter = ('is_available', 'city')
    search_fields = ('user__username', 'specialization', 'city')


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'specialist', 'category', 'price', 'duration_minutes', 'is_active')
    list_filter = ('category', 'is_active')
    search_fields = ('name', 'specialist__username')


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ('specialist', 'weekday', 'start_time', 'end_time', 'is_active')
    list_filter = ('weekday', 'is_active')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('client', 'specialist', 'service', 'date', 'start_time', 'status')
    list_filter = ('status', 'date')
    search_fields = ('client__username', 'specialist__username', 'service__name')


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('client', 'specialist', 'rating', 'created_at')
    list_filter = ('rating',)
    search_fields = ('client__username', 'specialist__username')
