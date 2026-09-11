"""Django Admin for appointments + booking engine models."""
from __future__ import annotations

from django import forms
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


class WorkingHoursInlineForm(forms.ModelForm):
    """Одна строка расписания — с проверкой ОБЕИХ сторон «выходного».

    Правило простое: выходной — это отсутствие часов, а не часы, которые
    никто не смотрит. Но держать его в этом репозитории было нечем:

    * у модели нет ни ``constraints``, ни ``clean`` — проверено чтением
      ``appointments/models.py``, а не памятью;
    * ``WorkingHoursSerializer.validate`` (``users/schedule_api.py``)
      заходит внутрь только при ``is_working_day=True``. Ветки «выходной,
      а времена заданы» там нет вовсе, и «выходной с 10 до 19» проходит
      через API сегодня.

    Сторож был односторонним: он охранял рабочий день и молчал про
    выходной. Здесь проверяются обе стороны, потому что вторая — это
    строка, которая ЧИТАЕТСЯ потребителем как «не работает», а выглядит
    в базе как заполненная смена, и разойтись они могут молча.

    Проверки рабочего дня повторяют сериализатор дословно, а не «примерно
    так же»: два разных набора правил на один объект — это способ
    получить строку, которую одна дверь принимает, а другая нет.
    """

    class Meta:
        model = SpecialistWorkingHours
        fields = ('day_of_week', 'is_working_day', 'start_time', 'end_time',
                  'break_start', 'break_end')

    def clean(self):
        cleaned = super().clean()
        working = cleaned.get('is_working_day')
        start = cleaned.get('start_time')
        end = cleaned.get('end_time')
        break_start = cleaned.get('break_start')
        break_end = cleaned.get('break_end')

        if not working:
            # Сторона, которой не было нигде. Молча обнулить времена за
            # человека нельзя: он мог ошибиться галочкой, а не полями, и
            # тихая очистка стёрла бы смену, которую он вводил.
            filled = {
                name: value
                for name, value in (
                    ('start_time', start), ('end_time', end),
                    ('break_start', break_start), ('break_end', break_end),
                )
                if value is not None
            }
            if filled:
                for name in filled:
                    self.add_error(
                        name,
                        'Выходной день не может иметь времён. Уберите время '
                        'или снимите отметку «выходной».',
                    )
            return cleaned

        if not start or not end:
            raise forms.ValidationError(
                'У рабочего дня должны быть начало и конец смены.'
            )
        if start >= end:
            raise forms.ValidationError('Начало смены должно быть раньше конца.')

        if break_start or break_end:
            if not (break_start and break_end):
                raise forms.ValidationError(
                    'Перерыв задаётся двумя границами: начало и конец.'
                )
            if break_start >= break_end:
                raise forms.ValidationError(
                    'Начало перерыва должно быть раньше его конца.'
                )
            if break_start < start or break_end > end:
                raise forms.ValidationError('Перерыв должен быть внутри смены.')

        return cleaned


class SpecialistWorkingHoursInline(admin.TabularInline):
    """Семь строк расписания рядом с мастером, а не отдельным экраном.

    Живёт здесь, рядом с ``SpecialistWorkingHoursAdmin``, а монтируется в
    ``users.admin.SpecialistProfileAdmin`` — тем же приёмом, которым
    ``TenantMastersInline`` живёт рядом с админкой мастера и монтируется
    в салон: правила у одного объекта обязаны быть одни и те же, где бы
    его ни заводили.

    Заводя салон из пяти мастеров, человек делал тридцать пять отправок
    формы, которая про мастера даже не упоминает. Здесь он делает пять.

    ``extra = 0`` намеренно. Пустые строки — это приглашение заполнить
    семь дней руками, а руками их заполнять и не нужно: для этого есть
    действие-пресет. Пустая форма, которую предлагают заполнить, —
    ровно тот путь, которым в базу попадают выдуманные часы.
    """

    model = SpecialistWorkingHours
    form = WorkingHoursInlineForm
    extra = 0
    ordering = ('day_of_week',)
    verbose_name = 'День недели'
    verbose_name_plural = 'Рабочие часы'


@admin.register(SpecialistWorkingHours)
class SpecialistWorkingHoursAdmin(admin.ModelAdmin):
    form = WorkingHoursInlineForm
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
