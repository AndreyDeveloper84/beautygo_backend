"""Django Admin for appointments + booking engine models."""
from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from payments.models import Payment

from .models import (
    Appointment,
    OutboxEvent,
    SpecialistTimeOff,
    SpecialistWorkingHours,
)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = (
        'id_short', 'client', 'specialist_name', 'status_badge',
        'start_datetime', 'price', 'is_first_visit', 'created_at',
    )
    list_filter = ('status', 'is_first_visit', 'start_datetime')
    search_fields = (
        'client__phone', 'specialist__display_name', 'idempotency_key',
    )
    readonly_fields = (
        'id', 'idempotency_key', 'is_first_visit',
        'snapshot_service_name', 'snapshot_duration_minutes',
        'snapshot_price', 'snapshot_commission_percent',
        'snapshot_specialist_income', 'snapshot_platform_fee',
        'snapshot_timezone', 'created_at', 'updated_at',
    )
    ordering = ('-start_datetime',)
    date_hierarchy = 'start_datetime'
    raw_id_fields = ('client', 'specialist', 'service', 'cancelled_by')

    fieldsets = (
        ('Основное', {
            'fields': (
                'id', 'client', 'specialist', 'service', 'status',
                'start_datetime', 'end_datetime', 'price', 'notes',
            ),
        }),
        ('Booking Engine', {
            'fields': (
                'idempotency_key', 'is_first_visit',
                'cancellation_reason', 'cancelled_by',
            ),
        }),
        ('Snapshot (на момент бронирования)', {
            'fields': (
                'snapshot_service_name', 'snapshot_duration_minutes',
                'snapshot_price', 'snapshot_commission_percent',
                'snapshot_specialist_income', 'snapshot_platform_fee',
                'snapshot_timezone',
            ),
            'classes': ('collapse',),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='ID')
    def id_short(self, obj: Appointment) -> str:
        return str(obj.id)[:8]

    @admin.display(description='Мастер')
    def specialist_name(self, obj: Appointment) -> str:
        return obj.specialist.display_name

    _STATUS_COLORS = {
        'pending': '#f59e0b',
        'awaiting_payment': '#3b82f6',
        'confirmed': '#22c55e',
        'in_progress': '#8b5cf6',
        'completed': '#6b7280',
        'cancelled': '#ef4444',
        'no_show': '#dc2626',
    }

    @admin.display(description='Статус')
    def status_badge(self, obj: Appointment) -> str:
        color = self._STATUS_COLORS.get(obj.status, '#888')
        return format_html(
            '<span style="color:{};font-weight:bold">{}</span>',
            color, obj.get_status_display(),
        )


@admin.register(SpecialistWorkingHours)
class SpecialistWorkingHoursAdmin(admin.ModelAdmin):
    list_display = ('specialist', 'day_of_week', 'is_working_day', 'start_time', 'end_time')
    list_filter = ('is_working_day', 'day_of_week')
    search_fields = ('specialist__display_name',)
    raw_id_fields = ('specialist',)


@admin.register(SpecialistTimeOff)
class SpecialistTimeOffAdmin(admin.ModelAdmin):
    list_display = ('specialist', 'start_at', 'end_at', 'reason')
    search_fields = ('specialist__display_name', 'reason')
    raw_id_fields = ('specialist',)
    ordering = ('-start_at',)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('short_id', 'appointment_short', 'amount', 'status', 'provider', 'created_at')
    list_filter = ('status', 'provider')
    search_fields = ('provider_payment_id',)
    readonly_fields = (
        'id', 'appointment', 'amount', 'specialist_income', 'platform_fee',
        'refunded_amount', 'provider', 'provider_payment_id',
        'last_webhook_event_id', 'created_at', 'updated_at',
    )

    @admin.display(description='ID')
    def short_id(self, obj: Payment) -> str:
        return str(obj.id)[:8]

    @admin.display(description='Запись')
    def appointment_short(self, obj: Payment) -> str:
        return str(obj.appointment_id)[:8]


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ('topic', 'status_display', 'error_count', 'created_at', 'processed_at')
    list_filter = ('topic',)
    readonly_fields = (
        'id', 'topic', 'payload', 'created_at', 'processed_at',
        'error_count', 'last_error',
    )
    ordering = ('-created_at',)

    @admin.display(description='Статус')
    def status_display(self, obj: OutboxEvent) -> str:
        if obj.processed_at:
            return format_html('<span style="color:#22c55e">Processed</span>')
        if obj.error_count >= 5:
            return format_html('<span style="color:#ef4444">Dead letter</span>')
        return format_html('<span style="color:#f59e0b">Pending</span>')
