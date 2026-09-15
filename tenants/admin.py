"""Admin registration for Tenant (DRF-242.1).

Uses Unfold-styled admin to match the rest of the project. Read-only on
``id`` and the timestamps; everything else is editable so a maintainer can
rename / deactivate a tenant from the admin without a migration.
"""
from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers as admin_helpers
from django.core.exceptions import NON_FIELD_ERRORS, FieldDoesNotExist, ValidationError
from django.shortcuts import render
from django.utils import timezone

from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.admin import TenantMastersInline


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    # DRF-1596 — до этого тикета кортеж был пуст, и владелец, открыв
    # `/admin/tenants/tenant/add/`, чтобы завести настоящий салон с
    # мастерами, добавить мастера не мог: форма салона про мастеров не
    # знала вовсе. Блок определён в `users.admin` (рядом с моделью и с
    # `SpecialistProfileAdmin`), чтобы подсказки и набор полей у мастера
    # были одни и те же, где бы его ни заводили.
    inlines = (TenantMastersInline,)

    list_display = ("name", "slug", "city", "is_active", "created_at")
    list_filter = ("is_active", "city")
    search_fields = ("name", "slug", "city", "address")
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("id", "slug", "name", "is_active")}),
        # DRF-1587 — единственное место, где адрес и город салона можно
        # завести в Ayla. До этого тикета их не было в источнике вовсе:
        # город существовал только в зеркале бота, куда его вписывал
        # оператор (`create_tenant --city`), а адрес — на профиле каждого
        # мастера отдельной копией одной и той же строки.
        ("Адрес", {
            "fields": ("city", "address"),
            "description": (
                "Пустое поле означает «не указано» и уезжает наружу как "
                "null. Салон без города не попадает ни в один городской "
                "ответ поиска."
            ),
        }),
        ("Системное", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        # Admin must see deactivated tenants too — use all_objects manager.
        return Tenant.all_objects.all()


class ModelErrorsShownOnTheForm(forms.ModelForm):
    """Ошибка модели на поле, которого в форме нет, — видна, а не 500.

    ``ModelForm`` кладёт ошибку ``clean()`` модели на поле по имени. Если поля
    в форме нет (оно только для чтения или не выведено в инлайн),
    ``add_error`` бросает ``ValueError``, и оператор видит 500 вместо причины.

    Здесь такая ошибка переносится в ошибки формы без поля, с названием поля
    впереди. Текст модели сохраняется дословно, форма остаётся невалидной, и
    строка не сохраняется. Ошибки на полях, которые в форме есть, не трогаются.
    """

    def _update_errors(self, errors):
        if hasattr(errors, "error_dict"):
            shown: dict[str, list] = {}
            for field, field_errors in errors.error_dict.items():
                if field == NON_FIELD_ERRORS or field in self.fields:
                    shown.setdefault(field, []).extend(field_errors)
                    continue
                try:
                    label = self._meta.model._meta.get_field(field).verbose_name
                except FieldDoesNotExist:
                    label = field
                shown.setdefault(NON_FIELD_ERRORS, []).extend(
                    f"{label}: {text}" for text in ValidationError(field_errors).messages
                )
            errors = ValidationError(shown)
        super()._update_errors(errors)


def _confirmed_with_provenance(place) -> bool:
    return bool(
        place.status == LocationStatus.CONFIRMED
        and place.confirmed_by_id
        and place.confirmed_at
        and place.confirmed_source_ref
    )


class ServiceLocationInlineForm(ModelErrorsShownOnTheForm):
    """Строка места в инлайне салона: «confirmed» здесь не ставится.

    Решение владельца: не ставить CONFIRMED без ``confirmed_by`` /
    ``confirmed_at`` / ``confirmed_source_ref``. В строке инлайна этих полей
    нет, поэтому месту, которое ещё не подтверждено с происхождением, вариант
    «confirmed» не предлагается. Попытка его прислать — ошибка на ``status``.
    Подтверждённое место своё значение сохраняет.
    """

    class Meta:
        model = ServiceLocation
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        status = self.fields.get("status")
        if status is None or _confirmed_with_provenance(self.instance):
            return
        status.choices = [c for c in status.choices if c[0] != LocationStatus.CONFIRMED]
        status.error_messages["invalid_choice"] = (
            "Подтвердить место в строке салона нельзя: нужны кто, когда и на каком "
            "основании. Используйте действие «Подтвердить место (я)» в списке мест "
            "или форму места."
        )


class ServiceLocationAdminForm(ModelErrorsShownOnTheForm):
    class Meta:
        model = ServiceLocation
        fields = "__all__"


@admin.action(description="Подтвердить место (я)")
def confirm_location_as_me(modeladmin, request, queryset):
    """Подтвердить выбранные места от своего имени — с названным основанием.

    Кто — нажавший, когда — момент записи. Основание называет человек на
    промежуточной странице: поле обязательно, умолчания нет, пустая строка и
    пробелы — не основание. Запись действия основанием не является (§73).

    Результат называется по каждому месту: подтверждено; уже подтверждено и
    не тронуто; отказ с причиной модели. Частичный успех называется отдельно.
    """
    places = list(queryset.select_related("tenant"))
    raw_basis = request.POST.get("source_ref", "")
    basis = raw_basis.strip()
    second_step = request.POST.get("confirm") == "yes"

    if not second_step or not basis:
        return render(request, "admin/tenants/confirm_location_as_me.html", {
            "title": "Подтвердить место (я)",
            "places": places,
            "basis": raw_basis,
            "basis_error": second_step and not basis,
            "confirmer": request.user,
            "action_checkbox_name": admin_helpers.ACTION_CHECKBOX_NAME,
            "opts": modeladmin.model._meta,
            "media": modeladmin.media,
        })

    now = timezone.now()
    confirmed, already, refused = [], [], []
    for place in places:
        if place.status == LocationStatus.CONFIRMED:
            already.append(place)
            continue
        place.status = LocationStatus.CONFIRMED
        place.confirmed_by = request.user
        place.confirmed_at = now
        place.confirmed_source_ref = basis
        try:
            place.full_clean()
        except ValidationError as exc:
            refused.append((place, "; ".join(exc.messages)))
            continue
        place.save(update_fields=[
            "status", "confirmed_by", "confirmed_at", "confirmed_source_ref", "updated_at",
        ])
        confirmed.append(place)

    if confirmed:
        modeladmin.message_user(
            request,
            f"Подтверждено ({len(confirmed)}), основание «{basis}»: "
            + ", ".join(str(p) for p in confirmed) + ".",
            messages.SUCCESS,
        )
    if already:
        modeladmin.message_user(
            request,
            "Уже подтверждены — не тронуты, прежнее основание сохранено: "
            + ", ".join(str(p) for p in already) + ".",
            messages.WARNING,
        )
    for place, reason in refused:
        modeladmin.message_user(
            request, f"Не подтверждено — {place}: {reason}", messages.ERROR,
        )
    if confirmed and (already or refused):
        modeladmin.message_user(
            request,
            f"Частичный результат: подтверждено {len(confirmed)} из {len(places)}.",
            messages.WARNING,
        )
    return None


class ServiceLocationInline(admin.TabularInline):
    """Места салона — рядом с ним, чтобы подтверждающий видел, что уже есть."""

    model = ServiceLocation
    form = ServiceLocationInlineForm
    extra = 0
    fields = ("label", "address", "city", "status", "geocode_status", "latitude", "longitude")
    readonly_fields = ("geocode_status", "latitude", "longitude")
    show_change_link = True


TenantAdmin.inlines = TenantAdmin.inlines + (ServiceLocationInline,)


@admin.register(ServiceLocation)
class ServiceLocationAdmin(admin.ModelAdmin):
    """Очередь подтверждения мест (§9, DRF-1687).

    Фильтр по ``status`` — рабочий инструмент: «покажи всё, что требует
    проверки». Подтверждение без автора, времени и основания форма не
    пропустит (``clean()`` модели), а ``update()`` мимо формы — не пропустит
    схема (``servicelocation_confirmed_requires_provenance``).

    Координаты и восемь полей происхождения — только на чтение: их пишет
    геокодер (``core.geocoding``), не человек. Человек подтверждает МЕСТО;
    подтверждение КООРДИНАТ неоднозначного адреса — отдельная поверхность,
    которой пока нет ни у кого (см. DRF-1349).
    """

    list_display = (
        "__str__", "tenant", "city", "status", "geocode_status",
        "confirmed_by", "confirmed_at", "updated_at",
    )
    list_filter = ("status", "geocode_status", "tenant", "city")
    search_fields = ("label", "address", "city", "tenant__slug", "tenant__name",
                     "geocode_normalized_address")
    raw_id_fields = ("tenant", "confirmed_by")
    form = ServiceLocationAdminForm
    actions = (confirm_location_as_me,)
    readonly_fields = (
        "id", "geocode_source_address", "geocode_normalized_address",
        "latitude", "longitude", "geocode_provider", "geocode_precision",
        "geocode_status", "geocoded_at", "created_at", "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("id", "tenant", "label", "address", "city")}),
        ("Подтверждение места (§9)", {
            "fields": ("status", "confirmed_by", "confirmed_at", "confirmed_source_ref"),
            "description": (
                "confirmed требует всех трёх полей: кто, когда, на каком основании. "
                "review_required — происхождение неизвестно. inactive — тестовый, "
                "личный или недействительный адрес."
            ),
        }),
        ("Координаты и происхождение (§139) — пишет геокодер", {
            "fields": (
                "geocode_status", "latitude", "longitude", "geocode_precision",
                "geocode_provider", "geocode_source_address",
                "geocode_normalized_address", "geocoded_at",
            ),
            "classes": ("collapse",),
        }),
        ("Системное", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tenant", "confirmed_by")
