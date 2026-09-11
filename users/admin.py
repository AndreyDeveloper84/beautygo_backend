"""Django Admin configuration for users app."""
from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import ValidationError
from django.utils.html import format_html

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
    "Координаты необязательны и сегодня пусты у всех мастеров пилота — "
    "заполнять их наугад нельзя. Адрес остаётся полем мастера, но клиенту "
    "показывается адрес салона (DRF-1587): для мастера в салоне это поле "
    "дублирует салонное и вводит в заблуждение. Правило старшинства — "
    "DRF-1589."
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
    actions = [approve_specialists, reject_specialists]

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
    raw_id_fields = ('user',)

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
            'fields': ('experience_years', 'address', 'location_lat', 'location_lng'),
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
    """

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
