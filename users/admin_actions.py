"""Действие админки «Связать с Ayla» для внешней личности без связи (DRF-1509).

§148: регистрация соло-мастера в MAX-боте ПРОБУЕТ связаться с Ayla сама,
не выходит — падает в ``SETUP_PENDING`` и ждёт, оператор добивает руками.
До этого модуля «руками» означало Django-shell: ручка
``POST /internal/users/bind-external/`` закрыта провижининг-токеном,
которого на пилоте нет, а сам бот к ней не допущен по построению.

Здесь оператору даётся кнопка, и она делает **ровно то же**, что ручка,
тем же сервисом — ``users.services.bind_external_identity`` через
операторскую обёртку ``bind_external_identity_by_operator``. Ни одной
строки логики связывания тут нет: две копии разъехались бы.

Что видит оператор
------------------

Список ``PendingExternalIdentity`` — прокси-строки ``is_proxy=True`` без
``linked_user``. Это единственный носитель «ждёт связывания» на стороне
каталога (``SETUP_PENDING`` живёт в боте). Каталог не отличает
регистрирующегося соло-мастера от клиента за такой же строкой: оператор
сопоставляет внешний id (``bot:max:<id>``) со списком ``SETUP_PENDING``
в админке бота.

Выбирает одну строку → действие → промежуточная страница с выбором
мастера → подтверждение. После успеха ``GET /internal/me/identity/`` для
той же личности возвращает ``is_proxy=false`` и настоящий ``ayla_user_id``
мастера — ровно то, что читает автоматика бота (S2), и состояние в боте
переезжает из ``SETUP_PENDING`` само.

Три отказа, три разных сообщения (§148 retryable, §143 fail-closed):

* **человек не найден** — выбранная строка мастера не является активным
  аккаунтом специалиста;
* **уже связан** — прокси уже указывает на аккаунт; повтор ничего не
  пишет и не заводит второй привязки;
* **нет прокси-строки** — строка исчезла или перестала быть прокси между
  открытием формы и подтверждением; связывать нечего.

Актор — ``request.user``; сервис его **требует**, а не подставляет: без
аутентифицированного пользователя связывание не выполняется
(``IdentityBindingActorRequiredError``), и это проверено положительным
контролем в тестах.
"""
from __future__ import annotations

import logging

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse

from .models import PendingExternalIdentity, SpecialistProfile, User
from .services import (
    BindTargetNotFoundError,
    ExternalIdentityAlreadyBoundError,
    ExternalIdentityNotFoundError,
    IdentityBindingActorRequiredError,
    IdentityBindingConflictError,
    InvalidExternalUserIDError,
    bind_external_identity_by_operator,
)

logger = logging.getLogger(__name__)

#: Имя действия — по нему промежуточная форма возвращается в changelist.
LINK_ACTION_NAME = "link_to_ayla"

#: Тексты отказов. Три причины — три разных строки, чтобы оператор не
#: получал «ничего не произошло» и не гадал, что делать дальше.
MSG_TARGET_NOT_FOUND = (
    "Человек не найден: выбранная строка не является активным аккаунтом "
    "специалиста (роль specialist, не прокси, не удалён, не заблокирован). "
    "Связывание не выполнено."
)
MSG_ALREADY_BOUND = (
    "Уже связан: личность {external_id} уже указывает на аккаунт "
    "{linked}. Повторное связывание не выполнено, второй привязки нет."
)
MSG_NO_PROXY_ROW = (
    "Нет прокси-строки: для {external_id} в каталоге нет строки-прокси "
    "(бот ещё не обращался в Ayla с этой личностью, либо строка уже "
    "изменилась). Связывать нечего."
)
MSG_ACTOR_REQUIRED = (
    "Действие не выполнено: не удалось надёжно определить оператора "
    "(§143 — без автора запись в журнал не делается, а без записи "
    "связывание не выполняется)."
)
MSG_SELECT_ONE = (
    "Выберите ровно одну личность: связывание — операция «одна личность → "
    "один мастер», выбрано {count}."
)
MSG_SUCCESS = (
    "Связано: {external_id} → {master} ({user_id}). Теперь "
    "GET /internal/me/identity/ для этой личности вернёт is_proxy=false и "
    "ayla_user_id={user_id}; запись в журнале: оператор {actor}."
)

PANEL_HELP = (
    "Здесь только строки, которые ждут оператора: внешняя личность "
    "(бот уже обращался в Ayla от её имени, строка-прокси заведена), но "
    "с настоящим аккаунтом она не связана. Каталог не знает, кто за ней — "
    "регистрирующийся соло-мастер или клиент: сверяйте id со списком "
    "SETUP_PENDING в админке бота. После связывания строка отсюда "
    "пропадает, а бот при следующем обращении получает настоящий ключ."
)


def external_id_channel(username: str) -> str:
    """``bot:max:12345`` → ``max``; одно-сегментная форма → ``—``."""
    parts = (username or "").split(":")
    return parts[1] if len(parts) >= 3 else "—"


class LinkTargetForm(forms.Form):
    """Промежуточная форма: какого мастера связать с выбранной личностью."""

    target = forms.ModelChoiceField(
        label="Мастер (аккаунт специалиста в Ayla)",
        queryset=SpecialistProfile.objects.none(),
        help_text=(
            "Показаны только активные аккаунты специалистов. Связывание "
            "одностороннее: перепривязать к другому человеку потом нельзя "
            "без отдельной операции снятия связи."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].queryset = (
            SpecialistProfile.objects
            .select_related("user", "tenant")
            .filter(
                user__role="specialist", user__is_proxy=False,
                user__is_active=True, user__deleted_at__isnull=True,
            )
            .order_by("display_name")
        )
        self.fields["target"].label_from_instance = _target_label


def _target_label(profile: SpecialistProfile) -> str:
    tenant = profile.tenant.name if profile.tenant_id else "без салона"
    phone = profile.user.phone or "без телефона"
    return f"{profile.display_name} — {phone} — {tenant}"


@admin.action(description="Связать с Ayla (выбрать мастера)", permissions=["link"])
def link_to_ayla(modeladmin, request, queryset):
    """Шаг 1 — показать форму выбора мастера; шаг 2 (``apply``) — связать.

    Оба шага приходят POST-ом в changelist: первый — от кнопки «Выполнить»
    Django-админки, второй — от промежуточной формы, которая возвращает
    те же ``action`` и ``_selected_action`` плюс ``apply=1``.
    """
    selected = request.POST.getlist(ACTION_CHECKBOX_NAME)
    if len(selected) != 1:
        modeladmin.message_user(
            request, MSG_SELECT_ONE.format(count=len(selected)), messages.ERROR,
        )
        return None
    (pk,) = selected

    if "apply" not in request.POST:
        # Строка берётся из отфильтрованного queryset действия — то есть
        # только та, что действительно ждёт. Если её там уже нет,
        # честно говорим об этом, а не показываем пустую форму.
        row = queryset.filter(pk=pk).first()
        if row is None:
            modeladmin.message_user(
                request, MSG_NO_PROXY_ROW.format(external_id=pk), messages.ERROR,
            )
            return None
        context = {
            **modeladmin.admin_site.each_context(request),
            "title": "Связать внешнюю личность с мастером Ayla",
            "opts": modeladmin.model._meta,
            "row": row,
            "channel": external_id_channel(row.username),
            "form": LinkTargetForm(),
            "action_name": LINK_ACTION_NAME,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "panel_help": PANEL_HELP,
        }
        return TemplateResponse(
            request, "admin/users/pendingexternalidentity/link_to_ayla.html",
            context,
        )

    form = LinkTargetForm(request.POST)
    if not form.is_valid():
        # Единственная ошибка формы — мастер не выбран или выбран не из
        # разрешённого набора; это тот же отказ «человек не найден».
        modeladmin.message_user(request, MSG_TARGET_NOT_FOUND, messages.ERROR)
        return None
    target_profile = form.cleaned_data["target"]

    # На шаге подтверждения строка ищется среди ВСЕХ прокси, а не только
    # ждущих: если её успели связать (двойной клик, вторая вкладка),
    # оператор должен прочитать «уже связан», а не «ничего не найдено».
    proxy = User.objects.filter(pk=pk, is_proxy=True).first()
    if proxy is None:
        modeladmin.message_user(
            request, MSG_NO_PROXY_ROW.format(external_id=pk), messages.ERROR,
        )
        return None

    return _perform_link(modeladmin, request, proxy, target_profile)


def _perform_link(modeladmin, request, proxy: User, target_profile: SpecialistProfile):
    external_id = proxy.username
    request_id = getattr(request, "request_id", None)
    try:
        bound = bind_external_identity_by_operator(
            external_id, target_profile.user_id,
            actor=request.user, request_id=request_id,
        )
    except IdentityBindingActorRequiredError:
        logger.warning(
            "admin.link_to_ayla.refused reason=actor_required external_user_id=%s",
            external_id,
        )
        modeladmin.message_user(request, MSG_ACTOR_REQUIRED, messages.ERROR)
        return None
    except ExternalIdentityNotFoundError:
        modeladmin.message_user(
            request, MSG_NO_PROXY_ROW.format(external_id=external_id), messages.ERROR,
        )
        return None
    except ExternalIdentityAlreadyBoundError:
        proxy.refresh_from_db(fields=["linked_user"])
        modeladmin.message_user(
            request,
            MSG_ALREADY_BOUND.format(
                external_id=external_id, linked=proxy.linked_user_id or "?",
            ),
            messages.ERROR,
        )
        return None
    except IdentityBindingConflictError as exc:
        # Гонка: строку связали между нашей проверкой и блокировкой в
        # сервисе, либо внешний id столкнулся с настоящим аккаунтом.
        modeladmin.message_user(
            request,
            MSG_ALREADY_BOUND.format(external_id=external_id, linked=str(exc)),
            messages.ERROR,
        )
        return None
    except (BindTargetNotFoundError, InvalidExternalUserIDError):
        modeladmin.message_user(request, MSG_TARGET_NOT_FOUND, messages.ERROR)
        return None

    modeladmin.log_change(
        request, proxy,
        f"Связана с мастером {target_profile.display_name} "
        f"(user {target_profile.user_id}) действием «Связать с Ayla»",
    )
    modeladmin.message_user(
        request,
        MSG_SUCCESS.format(
            external_id=external_id,
            master=target_profile.display_name,
            user_id=bound.linked_user_id,
            actor=request.user.get_username(),
        ),
        messages.SUCCESS,
    )
    return HttpResponseRedirect(
        reverse("admin:users_pendingexternalidentity_changelist"),
    )


@admin.register(PendingExternalIdentity)
class PendingExternalIdentityAdmin(admin.ModelAdmin):
    """Только ждущие строки, только одно действие, ничего не редактируется."""

    actions = [link_to_ayla]
    list_display = ("username", "channel", "date_joined")
    list_display_links = None
    search_fields = ("username",)
    ordering = ("-date_joined",)
    # Ничего не заводится и не правится руками: единственный путь записи —
    # сервис. Удаление тоже закрыто — оно стёрло бы след обращения бота.
    list_per_page = 50

    def get_queryset(self, request):
        # Менеджер модели уже фильтрует; повторяем условие здесь явно,
        # чтобы admin не зависел от того, какой менеджер окажется первым.
        return (
            super().get_queryset(request)
            .filter(is_proxy=True, linked_user__isnull=True)
        )

    @admin.display(description="Канал")
    def channel(self, obj) -> str:
        return external_id_channel(obj.username)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_link_permission(self, request) -> bool:
        """Право на действие: то же, что менять пользователей.

        Django заводит для прокси-модели свои permissions, но оператор
        пилота ими не размечен; связывание — изменение ``users.User``
        (``linked_user``), и право спрашивается именно это.
        """
        return request.user.has_perm("users.change_user")

    def changelist_view(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "panel_help": PANEL_HELP}
        return super().changelist_view(request, extra_context=extra_context)
