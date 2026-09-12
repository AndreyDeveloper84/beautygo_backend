"""Django Admin configuration for users app."""
from __future__ import annotations

from datetime import date, time, timedelta

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers as admin_helpers
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render
from django.utils.html import format_html

from appointments.admin import SpecialistWorkingHoursInline

from .models import (
    DeletionRequest, DeviceToken, OTPCode, Profile, SocialAccount,
    SpecialistProfile, User,
)


# ─── Тексты подсказок (DRF-1596) ─────────────────────────────────────────────
#
# Владелец 08.09.2026 открыл `/admin/tenants/tenant/add/`, чтобы завести
# настоящий салон с мастерами, и не нашёл, где добавить мастера. Формой это
# и правда было невозможно: на форме салона мастеров не было вовсе, а на
# форме мастера не было салона. Из десяти полей `SpecialistProfile` с
# `blank=False` в форму не попадали пять — `timezone`, `is_booking_enabled`,
# `booking_source`, `rating`, `reviews_count`, — и каждое подставлялось
# умолчанием модели молча.
#
# Образец подсказки — раздел «Адрес» в `TenantAdmin` (DRF-1587): он не
# описывает поле, он называет последствие незаполнения. Здесь то же самое.
# Один и тот же текст стоит и на отдельной форме мастера, и во вложенном
# блоке на форме салона — оператор не должен угадывать, что правила разные.

TENANT_HELP = (
    "Салон, которому принадлежит мастер. Пусто = мастер ничей: зеркало "
    "бота тянет каталог по одному салону за раз (?tenant=<uuid>, "
    "DRF-1313), и строка без салона не попадает ни в одну выборку — "
    "клиент такого мастера не увидит нигде. Адрес и город клиенту "
    "показываются салонные (DRF-1587)."
)

VISIBILITY_HELP = (
    "Эти поля решают, попадёт ли мастер в каталог. Клиенту отдаются "
    "ТОЛЬКО строки со «Статусом = Активен» И «Доступен = да» И активным "
    "пользователем. Умолчание статуса — «Черновик»: только что заведённый "
    "мастер клиенту НЕ виден, пока статус не переключат руками. "
    "«Принимает записи» каталог не фильтрует — оно ставит запись на паузу, "
    "оставляя карточку видимой. И даже после этого мастер доедет до "
    "клиента не мгновенно: в зеркале бота строка появляется со статусом "
    "приглашения «pending» и до подтверждения оператором в админке бота "
    "не продаётся (DRF-1496) — это отдельный шаг, отсюда его сделать "
    "нельзя."
)

BOOKING_HELP = (
    "Часовой пояс — тот, в котором считаются расписание и свободные окна "
    "мастера; ошибка здесь смещает все слоты. Источник записи — где живёт "
    "расписание: «Ayla local DB SoR» значит, что слоты и брони ведутся "
    "здесь (так у всех мастеров пилота); «YClients SoR» — что их ведёт "
    "YClients, и тогда обязательны идентификаторы компании и сотрудника "
    "YClients (они на отдельной форме мастера), иначе запись не "
    "оформится."
)

LOCATION_HELP = (
    "Место оказания услуг (§9, DRF-1687) — то, до чего считается расстояние. "
    "Для мастера салона выберите место этого салона, для самостоятельного — "
    "его собственную точку (заводится в «Места оказания услуг»). Пусто — "
    "расстояние неизвестно, не ноль. Адрес и координаты ниже — старые поля "
    "мастера: после §9 они не авторитетны, читатели переводятся на место, "
    "заполнять их не нужно."
)

STATS_HELP = (
    "Считает платформа по отзывам, руками не вводится. 0.0 при нуле "
    "отзывов означает «оценки ещё нет», а не «оценили на ноль»."
)

USER_ROLE_HELP = (
    "Пользователь мастера. Заводить его удобнее сразу с ролью «specialist»: "
    "тогда профиль мастера создаётся вместе с пользователем, и здесь "
    "достаточно выбрать его — существующая строка будет дополнена, а не "
    "продублирована. С другой ролью каталог мастера всё равно покажет, но "
    "в кабинет мастера человек не войдёт."
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


# ─── Пресет расписания (срез админской поверхности) ──────────────────────────
#
# Чтобы завести салон из пяти мастеров, человек делал ТРИДЦАТЬ ПЯТЬ отправок
# формы: семь строк расписания на каждого, отдельным экраном, который про
# мастера даже не упоминает. Вложенный блок на форме мастера убирает экран;
# это действие убирает семь отправок из пяти.
#
# ПОЧЕМУ СО СТРАНИЦЕЙ ПОДТВЕРЖДЕНИЯ, А НЕ ОДНИМ НАЖАТИЕМ
#
# Четверо пилотных мастеров числятся работающими семь дней с 10:00 до 19:00
# без обеда. Никто этого не вводил — это изготовленное умолчание прежнего
# кода, и по нему сегодня продают клиенту. Второй раз заводить механизм,
# который пишет часы за человека, нельзя: пресет обязан быть решением,
# которое видно ДО записи, а не значением, которое появилось само.
#
# Поэтому действие сначала показывает, ЧТО именно оно напишет, и кому, и у
# кого часы уже есть. Записывает — только после подтверждения.

PRESET_WEEKDAYS = (0, 1, 2, 3, 4)
PRESET_START = time(10, 0)
PRESET_END = time(19, 0)


def _preset_rows(specialist):
    """Семь строк пресета: Пн–Пт 10:00–19:00, Сб и Вс — выходные.

    Выходные приезжают СТРОКАМИ, а не отсутствием строк. Отсутствие строки
    и «выходной» читаются потребителем одинаково только до первого вопроса
    «а мы вообще заводили этому мастеру расписание»; строка на выходной
    отвечает на него, пустота — нет.
    """

    from appointments.models import SpecialistWorkingHours

    return [
        SpecialistWorkingHours(
            specialist=specialist,
            day_of_week=day,
            is_working_day=day in PRESET_WEEKDAYS,
            start_time=PRESET_START if day in PRESET_WEEKDAYS else None,
            end_time=PRESET_END if day in PRESET_WEEKDAYS else None,
            break_start=None,
            break_end=None,
        )
        for day in range(7)
    ]


@admin.action(description='🕘 Поставить расписание Пн–Пт 10:00–19:00')
def apply_default_schedule(modeladmin, request, queryset):
    """Пресет недели на выбранных мастеров — через подтверждение.

    Возвращает страницу подтверждения на первом заходе и пишет только на
    втором, когда человек увидел и часы, и список тех, у кого расписание
    уже есть.

    **По умолчанию существующее НЕ перезаписывается.** Перезапись —
    отдельная отметка, выключенная. Довод: в API замена всех семи дней
    (`PUT /schedule`) — явное намерение вызывающего, он прислал всю
    неделю; в админке «выделить всё» ставится одним движением, и молчаливое
    затирание чужого настоящего графика было бы тем же изготовленным
    умолчанием, только поверх данных, а не вместо них.
    """

    from appointments.models import SpecialistWorkingHours

    specialists = list(queryset.select_related('user'))
    with_hours = {
        row.specialist_id
        for row in SpecialistWorkingHours.objects.filter(
            specialist__in=specialists,
        ).only('specialist_id')
    }

    if request.POST.get('confirm') != 'yes':
        # Первый заход: показать, что будет написано, и кому.
        return render(request, 'admin/users/apply_default_schedule.html', {
            'title': 'Поставить расписание Пн–Пт 10:00–19:00',
            'queryset': specialists,
            'untouched': [s for s in specialists if s.pk in with_hours],
            'fresh': [s for s in specialists if s.pk not in with_hours],
            'preset_start': PRESET_START,
            'preset_end': PRESET_END,
            'action_checkbox_name': admin_helpers.ACTION_CHECKBOX_NAME,
            'opts': modeladmin.model._meta,
            'media': modeladmin.media,
        })

    overwrite = request.POST.get('overwrite') == 'yes'
    written = 0
    skipped = []

    for specialist in specialists:
        if specialist.pk in with_hours and not overwrite:
            skipped.append(specialist)
            continue
        with transaction.atomic():
            if specialist.pk in with_hours:
                SpecialistWorkingHours.objects.filter(specialist=specialist).delete()
            SpecialistWorkingHours.objects.bulk_create(_preset_rows(specialist))
        _invalidate_specialist_slots(specialist.pk)
        written += 1

    modeladmin.message_user(
        request,
        f'Расписание поставлено мастерам: {written}.',
        messages.SUCCESS,
    )
    if skipped:
        # Пропуск называется поимённо, а не числом: «пропущено 3» человек
        # прочитает как сбой, а список — как решение, которое он принял.
        names = ', '.join(str(s) for s in skipped)
        modeladmin.message_user(
            request,
            f'Пропущены — расписание уже есть, перезапись не отмечена: {names}.',
            messages.WARNING,
        )
    return None


def _invalidate_specialist_slots(specialist_id) -> None:
    """Погасить кэш слотов на весь горизонт записи.

    Тем же вызовом, что и писатель расписания в API
    (``users/schedule_api._invalidate_slots``): без него часы поменялись
    бы, а клиент продолжал видеть прежнюю сетку — расхождение, которое
    ничем не выдаёт себя, кроме жалобы клиента.

    Горизонт берётся из той же настройки ``BOOKING_MAX_AHEAD_DAYS``, а не
    из своего числа: два горизонта на один кэш разошлись бы молча.
    """

    from users.schedule_api import _invalidate_slots

    max_ahead = getattr(settings, 'BOOKING_MAX_AHEAD_DAYS', 60)
    today = date.today()
    _invalidate_slots(specialist_id, today, today + timedelta(days=max_ahead))


# ─── Inlines ─────────────────────────────────────────────────────────────────

class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = 'Профиль клиента'
    fk_name = 'user'
    fields = ('full_name', 'city', 'avatar', 'bio')


class TenantMasterInlineForm(forms.ModelForm):
    """Строка блока мастеров подхватывает профиль, а не дублирует его.

    Ловушка, из-за которой наивный inline не работал бы ни разу.
    ``users.signals.create_user_profile`` на создание
    ``User(role='specialist')`` заводит ``SpecialistProfile`` сам — то
    есть к моменту, когда оператор возвращается на форму салона и
    выбирает этого человека в строке блока, профиль на него уже есть.
    Связь ``SpecialistProfile.user`` — ``OneToOneField``, поэтому обычный
    inline упирался бы в «Specialist profile с таким User уже
    существует» и салон с мастерами не сохранялся бы никогда.

    Отсюда две обязанности:

    * **подхват** — строка формы приземляется на уже существующий
      профиль этого человека, а не пытается создать второй;
    * **отказ подхватывать чужого** — подхватывать разрешено только
      профиль без салона или профиль ЭТОГО же салона. Иначе форма
      чужого салона молча увела бы мастера к себе, и первый салон
      потерял бы его без единого следа.

    Обе стоят в ``_post_clean``, а не в ``clean()``, и это не вкусовщина:
    проверку уникальности запускает именно ``_post_clean`` (через
    ``instance.validate_unique()``), и она отрабатывает ПОСЛЕ ``clean()``.
    Подхват, сделанный в ``clean()``, до неё бы не доехал — форма падала
    бы на дубликате раньше.

    ``BaseInlineFormSet._construct_form`` заранее проставляет
    ``instance.tenant_id`` родительским салоном, поэтому сравнение ниже
    осмысленно и на форме создания: ``Tenant.id`` —
    ``UUIDField(default=uuid.uuid4)``, у несохранённого салона ``pk`` уже
    заполнен, и любой профиль с чужим непустым салоном отличается от
    него.
    """

    class Meta:
        model = SpecialistProfile
        fields = '__all__'

    def _post_clean(self):
        # ``_state.adding``, а НЕ ``instance.pk``: первичный ключ здесь
        # ``UUIDField(default=uuid.uuid4)``, поэтому у новой, ещё не
        # сохранённой строки он уже заполнен свежим uuid4, и проверка по
        # ``pk`` пропускала бы подхват всегда.
        user = self.cleaned_data.get('user')
        if user is not None and self.instance._state.adding:
            existing = (
                SpecialistProfile.objects.select_related('tenant')
                .filter(user=user)
                .first()
            )
            if existing is not None:
                owner = existing.tenant_id
                if owner is not None and owner != self.instance.tenant_id:
                    self.add_error('user', ValidationError(
                        'У пользователя «%(user)s» уже есть профиль мастера '
                        'в салоне «%(salon)s». Перевести мастера в другой '
                        'салон можно только на его собственной форме — '
                        'отсюда это молча увело бы его у первого салона.',
                        code='master_belongs_to_another_tenant',
                        params={'user': user, 'salon': existing.tenant},
                    ))
                    return
                # Подхват. Формой подменяется ВЕСЬ объект, а не только его
                # первичный ключ, и это принципиально: у пустой строки,
                # собранной формсетом, ``created_at`` равен ``None``, а в
                # блоке этого поля нет — Django собрал бы UPDATE, который
                # затирает дату заведения мастера в NULL и падает на
                # ``NOT NULL``. Взяв строку из базы, форма накладывает
                # введённые значения поверх настоящих, а всё, чего в блоке
                # нет (дата заведения, аватар, био, координаты, рейтинг),
                # остаётся как было.
                #
                # Салон переносится на подхваченную строку: его проставил
                # формсет, и именно он здесь и заводится.
                existing.tenant_id = self.instance.tenant_id
                self.instance = existing
        super()._post_clean()


class TenantMastersInline(admin.StackedInline):
    """Мастера салона прямо на форме салона (DRF-1596).

    Живёт здесь, рядом с ``SpecialistProfileAdmin``, а монтируется в
    ``tenants.admin.TenantAdmin``: подсказки и набор полей у одного и
    того же объекта обязаны быть одни и те же, где бы его ни заводили.
    """

    model = SpecialistProfile
    fk_name = 'tenant'
    form = TenantMasterInlineForm
    extra = 1
    verbose_name = 'Мастер'
    verbose_name_plural = 'Мастера салона'
    autocomplete_fields = ('user',)
    raw_id_fields = ('works_at',)
    show_change_link = True
    # Разделы, а не плоский список полей: подсказки нужны здесь даже
    # больше, чем на отдельной форме мастера. Владелец заводит салон
    # именно тут, и именно тут умолчание «Черновик» тише всего
    # превращает нового мастера в невидимого.
    fieldsets = (
        (None, {
            'fields': ('user', 'display_name', 'experience_years'),
            'description': USER_ROLE_HELP,
        }),
        ('Кого видит клиент', {
            'fields': ('status', 'is_available', 'is_booking_enabled'),
            'description': VISIBILITY_HELP,
        }),
        ('Где работает', {
            'fields': ('works_at',),
            'description': LOCATION_HELP,
        }),
        ('Приём записей', {
            'fields': ('timezone', 'booking_source'),
            'description': BOOKING_HELP,
        }),
    )


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
    readonly_fields = ('date_joined', 'last_login', 'is_proxy', 'linked_user')

    fieldsets = BaseUserAdmin.fieldsets + (
        ('BeautyGO', {'fields': ('role', 'phone', 'is_verified', 'deleted_at')}),
        # Phase C binding (E2E-BOT-02B): visible for ops investigation,
        # read-only — bind_external_identity / unlink_external_identity
        # in users/services.py are the ONLY write paths (managed,
        # audited operations per AYLA-DEC-0016 §4).
        ('Identity binding', {'fields': ('is_proxy', 'linked_user')}),
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
    actions = [approve_specialists, reject_specialists, apply_default_schedule]
    # Расписание — рядом с человеком, а не отдельным экраном. Инлайн живёт
    # в appointments.admin рядом с моделью и монтируется сюда: правила
    # у одного объекта обязаны быть одни и те же, где бы его ни правили.
    inlines = [SpecialistWorkingHoursInline]

    list_display = (
        'display_name', 'tenant', 'get_phone', 'status_badge',
        'rating', 'reviews_count', 'is_available', 'created_at',
    )
    list_filter = ('tenant', 'status', 'is_available', 'is_booking_enabled')
    list_select_related = ('user', 'tenant')
    search_fields = (
        'display_name', 'user__phone', 'user__username', 'address',
        'tenant__name', 'tenant__slug',
    )
    readonly_fields = ('rating', 'reviews_count', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('user', 'works_at')

    # DRF-1596. Порядок разделов повторяет порядок решений оператора:
    # чей мастер → кто он → увидит ли его клиент → как он принимает
    # записи → всё остальное. Раньше первых двух вопросов форма не
    # задавала вовсе: `tenant` в ней отсутствовал, а `timezone`,
    # `is_booking_enabled` и `booking_source` подставлялись умолчаниями
    # модели молча.
    fieldsets = (
        ('Салон', {
            'fields': ('tenant',),
            'description': TENANT_HELP,
        }),
        ('Основное', {
            'fields': ('user', 'display_name', 'avatar', 'bio'),
            'description': USER_ROLE_HELP,
        }),
        ('Кого видит клиент', {
            'fields': ('status', 'is_available', 'is_booking_enabled'),
            'description': VISIBILITY_HELP,
        }),
        ('Приём записей', {
            'fields': (
                'timezone', 'booking_source',
                'yclients_company_id', 'yclients_staff_id',
            ),
            'description': BOOKING_HELP,
        }),
        ('Опыт и локация', {
            'fields': ('experience_years', 'works_at', 'address', 'location_lat', 'location_lng'),
            'description': LOCATION_HELP,
        }),
        ('Статистика', {
            'fields': ('rating', 'reviews_count'),
            'classes': ('collapse',),
            'description': STATS_HELP,
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


@admin.register(DeletionRequest)
class DeletionRequestAdmin(admin.ModelAdmin):
    """Заявки на удаление — только чтение (DRF-1699, §7 свода).

    Статус меняет исполнитель, не рука оператора: правка статуса из
    админки сделала бы «завершено» без стирания, то есть ложный успех —
    ровно то, что §7 запрещает. Заводить заявку отсюда тоже нельзя: она
    заводится от имени человека его подтверждением.

    Единственное действие — «Исполнить сейчас» (D3, DRF-1725): ставит
    открытую заявку в очередь исполнителя, не дожидаясь тика. Это
    запуск того же исполнителя, а не правка статуса рукой.
    """

    actions = ("execute_now",)

    list_display = ("id", "user", "status", "requested_at", "deadline_at", "completed_at", "initiator")
    list_filter = ("status", "initiator")
    search_fields = ("id", "user__phone", "user__username")
    readonly_fields = (
        "id", "user", "status", "requested_at", "deadline_at", "started_at",
        "completed_at", "initiator", "steps", "failure_reason",
    )
    raw_id_fields = ("user",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Исполнить сейчас (открытые заявки)")
    def execute_now(self, request, queryset):
        from users.tasks import execute_deletion_request

        queued = 0
        for req in queryset.filter(status__in=DeletionRequest.OPEN_STATUSES):
            execute_deletion_request.delay(str(req.pk))
            queued += 1
        self.message_user(request, f"Поставлено в очередь исполнителя: {queued}.")


# Действие «Связать с Ayla» для внешних личностей без связи (DRF-1509, §148):
# регистрируется при импорте модуля.
from . import admin_actions  # noqa: E402,F401
