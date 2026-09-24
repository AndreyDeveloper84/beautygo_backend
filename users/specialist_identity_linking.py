"""Личность мастера, принявшего приглашение — связь без участия человека (DRF-2442).

Решение владельца §77 п.38 (24.09.2026): «мне не надо участия человека в
регистрации мастеров». До этого листа единственный путь записи непустого
``User.linked_user_id`` для ``role=specialist`` был ручным — действие оператора
«Связать с Ayla» в Django Admin каталога
(:func:`users.services.bind_external_identity_by_operator`), и выполнить его
было некому: группа операторов пуста. Поэтому кабинет мастера отвечал 403
``subject_unresolved`` на всех ручках субъекта, а у мастера, заведённого
салоном, — с самого начала: временная форточка
``provisioned_workspace_owner`` открыта только для СОЛО-профиля в DRAFT с
совпавшим claim'ом.

# Чем доказывается владение, и почему запрет не обойдён

В ``users/internal_users_api.py`` у s2s-ручки ``bind-external`` написано прямо:
«Production bot-driven binding is not supported until a verified ownership flow
exists», и её ``target_roles`` по умолчанию — ``("client",)``: специалиста она
связать не может в принципе. Этот запрет здесь **не снимается, а
удовлетворяется**: доказательство владения — **одноразовое приглашение**,
выписанное салоном на конкретного мастера и **погашенное на стороне бота**
(``master_api.views.onboarding_accept``: ``invite_token`` гасится, строка
получает ``linked_bot_user``). Открыть приглашение может лишь тот, кому его
передали; ссылка одноразовая, и второй предъявитель получает
``wrong_recipient``. Каталог доверяет не «боту вообще», а факту гашения — и
поэтому у ручки свой credential и своя сила, как у ``salon-admins``.

# Образец — ``salon-admins`` (DRF-2085), и он не случайный

Связка личности MAX **без человека** у нас уже работает для администратора
салона: :mod:`users.salon_admin_linking` под
:class:`users.permissions.IsSalonAdminLinkBearer`. Здесь та же форма: свой
токен, свой сторож, идемпотентность по ключу, аудит строкой в каталоге,
authoritative readback, именованные отказы. Различие одно и существенное:
**та ручка СОЗДАЁТ учётку, эта — не создаёт ничего.** Специалист и его
профиль уже есть (их завёл салон или провижининг соло); здесь только
появляется ребро личности к уже существующей строке.

# Что операция НЕ делает

* не создаёт ни ``User``, ни ``SpecialistProfile``, ни ``TenantUserRelationship``;
* не перепривязывает: личность, связанная с другой учёткой, — отказ
  ``identity_already_bound``, а не «перевесим»;
* не принимает ``role=client`` и никакую другую роль, кроме ``specialist``:
  ``target_roles=("specialist",)``. Универсальная дверь связывала бы кого
  угодно с кем угодно;
* не создаёт прокси-строку: связывать можно только личность, которую бот уже
  предъявлял каталогу (§148 — перевешивать надо ту строку, которую бот
  прочитает потом). Нет строки — ``identity_unknown``;
* не открывает выдачу. Связь открывает мастеру **кабинет**; попадание в подбор
  решают статус, расписание и услуги — другой предмет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from users.models import SpecialistIdentityLinkRequest, SpecialistProfile, User
from users.permissions import IsInternalBearerForSpecialistSubject
from users.services import (
    INITIATOR_BOT_SPECIALIST_IDENTITY_LINK,
    IdentityBindingError,
    InvalidExternalUserIDError,
    bind_external_identity,
    is_valid_external_user_id,
    resolve_external_user_readonly,
)

logger = logging.getLogger(__name__)

#: Единственная роль, которую принимает эта дверь. НЕ ``client`` (s2s-ручка с
#: её умолчанием — другая сила и другое доказательство владения) и НЕ ``admin``
#: (это ``salon-admins``).
LINK_TARGET_ROLES: tuple[str, ...] = ("specialist",)

REASON_INVALID_EXTERNAL_ID = "invalid_external_user_id"
REASON_SPECIALIST_NOT_FOUND = "specialist_not_found"
REASON_SPECIALIST_NOT_LINKABLE = "specialist_not_linkable"
REASON_IDENTITY_UNKNOWN = "identity_unknown"
REASON_IDENTITY_NOT_PROXY = "identity_not_proxy"
REASON_IDENTITY_ALREADY_BOUND = "identity_already_bound"
REASON_IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
REASON_BIND_REFUSED = "bind_refused"
REASON_READBACK_FAILED = "readback_failed"

#: HTTP-статус по причине. ``specialist_not_found`` — 404 вместе с «профиль
#: есть, но связывать его нельзя»? Нет: это разные утверждения, и они
#: отвечают разными кодами, иначе бот не отличит «не тот субъект» от «не наш
#: субъект».
STATUS_BY_REASON: dict[str, int] = {
    REASON_INVALID_EXTERNAL_ID: 400,
    REASON_SPECIALIST_NOT_FOUND: 404,
    REASON_SPECIALIST_NOT_LINKABLE: 409,
    REASON_IDENTITY_UNKNOWN: 404,
    REASON_IDENTITY_NOT_PROXY: 409,
    REASON_IDENTITY_ALREADY_BOUND: 409,
    REASON_IDEMPOTENCY_KEY_REUSED: 409,
    REASON_BIND_REFUSED: 409,
    REASON_READBACK_FAILED: 500,
}


class SpecialistIdentityLinkRefused(Exception):
    """Отказ с машинной причиной; при отказе ничего не записано."""

    def __init__(self, reason: str, **details) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details
        self.status_code = STATUS_BY_REASON[reason]


@dataclass(frozen=True)
class SpecialistIdentityLink:
    """Связанная личность: профиль, его рабочая учётка, прокси и строка запроса."""

    profile: SpecialistProfile
    user: User
    proxy: User
    request: SpecialistIdentityLinkRequest
    created: bool


def _refuse(reason: str, *, correlation_id: str, specialist_id, **details) -> SpecialistIdentityLinkRefused:
    # Внешний id в лог не пишется: ``pii_guard`` считает идентификатор канала
    # персональными данными. Зацепка — correlation_id, id профиля и причина.
    logger.warning(
        "users.specialist_identity_link.refused reason=%s specialist=%s correlation_id=%s details=%s",
        reason, specialist_id, correlation_id or "-", details,
    )
    return SpecialistIdentityLinkRefused(reason, **details)


def _readback(external_user_id: str, profile: SpecialistProfile) -> bool:
    """Открылся ли КАБИНЕТ — тем же путём, которым ходит боевой сторож.

    Проверяется не «значение записано», а то, что нужно мастеру: резолвер
    личности (:func:`resolve_external_user_readonly` — тот же
    ``_follow_binding``, что у рантайма) приводит к реальной учётке, и
    :meth:`IsInternalBearerForSpecialistSubject.subject_of` — та самая
    функция, которой сторож субъекта сравнивает URL с актором, — называет
    ЭТОТ профиль. Отдельный запрос к ``linked_user_id`` доказывал бы запись и
    не доказывал бы доступ.
    """

    resolved = resolve_external_user_readonly(external_user_id)
    if resolved is None or resolved.is_proxy or resolved.pk != profile.user_id:
        return False
    gate = IsInternalBearerForSpecialistSubject()
    return gate.subject_of(resolved) == str(profile.pk)


def _linkable_profile(specialist_id, *, correlation_id: str) -> SpecialistProfile:
    """Профиль, к личности которого вообще можно вести ребро.

    Два разных отказа, и они не сливаются: **нет такого профиля** —
    ``specialist_not_found``; профиль есть, но его учётка не годится в цель
    (не ``specialist``, прокси, выключена, удалена, салон выключен) —
    ``specialist_not_linkable``. Первое говорит «мы просим не про того»,
    второе — «про того, но связывать нечего»; слив их в один код, бот
    потерял бы именно то различие, из-за которого сегодня 403 у мастеров
    читается как одна беда.
    """

    profile = (
        SpecialistProfile.objects.select_related("user", "tenant")
        .filter(pk=specialist_id)
        .first()
    )
    if profile is None or profile.user_id is None:
        raise _refuse(
            REASON_SPECIALIST_NOT_FOUND, correlation_id=correlation_id, specialist_id=specialist_id,
        )
    user = profile.user
    tenant = profile.tenant
    unlinkable = (
        user.role != "specialist"
        or user.is_proxy
        or not user.is_active
        or user.deleted_at is not None
        or (tenant is not None and not tenant.is_active)
    )
    if unlinkable:
        raise _refuse(
            REASON_SPECIALIST_NOT_LINKABLE,
            correlation_id=correlation_id,
            specialist_id=specialist_id,
            role=user.role,
        )
    return profile


def link_specialist_identity(
    specialist_id,
    external_user_id: str,
    *,
    actor: str,
    idempotency_key: str,
    correlation_id: str = "",
) -> SpecialistIdentityLink:
    """Связать MAX-личность принявшего приглашение мастера с его учёткой.

    Идемпотентность двойная, и обе половины нужны:

    * по ``idempotency_key`` — повтор того же запроса возвращает ту же строку
      и те же id; тот же ключ с ДРУГИМ телом — ``idempotency_key_reused``;
    * по состоянию — личность, уже связанная с ЭТИМ специалистом, отвечает
      успехом без второй записи (сеть могла оборвать первый ответ), а
      связанная с ДРУГОЙ учёткой — ``identity_already_bound``. Без второй
      половины повтор с новым ключом заводил бы вторую попытку связи.

    Readback обязателен: ``SUCCESS`` только после того, как боевой путь
    сторожа субъекта отвечает «этот профиль — его» (:func:`_readback`).
    """

    if not is_valid_external_user_id(external_user_id):
        raise _refuse(
            REASON_INVALID_EXTERNAL_ID, correlation_id=correlation_id, specialist_id=specialist_id,
        )

    prior = SpecialistIdentityLinkRequest.objects.filter(
        idempotency_key=idempotency_key,
    ).select_related("profile", "user").first()
    if prior is not None:
        same = (
            str(prior.profile_id) == str(specialist_id)
            and prior.external_user_id == external_user_id
        )
        if not same:
            raise _refuse(
                REASON_IDEMPOTENCY_KEY_REUSED,
                correlation_id=correlation_id,
                specialist_id=specialist_id,
            )
        profile = _linkable_profile(specialist_id, correlation_id=correlation_id)
        if not _readback(external_user_id, profile):
            # Строка есть, а доступа нет — повтор с тем же ключом повторит
            # именно readback, и это честнее, чем отдать 200 по наличию строки.
            raise _refuse(
                REASON_READBACK_FAILED, correlation_id=correlation_id, specialist_id=specialist_id,
            )
        proxy = User.objects.filter(username=external_user_id).first()
        assert proxy is not None  # readback прошёл: строка существует
        return SpecialistIdentityLink(
            profile=profile, user=profile.user, proxy=proxy, request=prior, created=False,
        )

    profile = _linkable_profile(specialist_id, correlation_id=correlation_id)

    with transaction.atomic():
        # Под блокировкой, чтобы гонка двух вызовов на одну личность не
        # завела два ребра: второй увидит уже записанное состояние.
        proxy = User.objects.select_for_update().filter(username=external_user_id).first()
        if proxy is None:
            raise _refuse(
                REASON_IDENTITY_UNKNOWN, correlation_id=correlation_id, specialist_id=specialist_id,
            )
        if not proxy.is_proxy:
            raise _refuse(
                REASON_IDENTITY_NOT_PROXY, correlation_id=correlation_id, specialist_id=specialist_id,
            )
        if proxy.linked_user_id is not None and proxy.linked_user_id != profile.user_id:
            raise _refuse(
                REASON_IDENTITY_ALREADY_BOUND,
                correlation_id=correlation_id,
                specialist_id=specialist_id,
            )
        try:
            proxy, _created = bind_external_identity(
                external_user_id,
                profile.user_id,
                initiator=INITIATOR_BOT_SPECIALIST_IDENTITY_LINK,
                request_id=correlation_id or None,
                target_roles=LINK_TARGET_ROLES,
            )
        except (IdentityBindingError, InvalidExternalUserIDError) as exc:
            # Исключение внутри atomic откатывает и строку запроса вместе с ним.
            raise _refuse(
                REASON_BIND_REFUSED,
                correlation_id=correlation_id,
                specialist_id=specialist_id,
                bind=type(exc).__name__,
            ) from exc
        request_row = SpecialistIdentityLinkRequest.objects.create(
            idempotency_key=idempotency_key,
            profile=profile,
            user=profile.user,
            external_user_id=external_user_id,
            actor=actor,
            correlation_id=correlation_id or "",
            result="created",
        )

    if not _readback(external_user_id, profile):
        # Связь записана, но кабинет не открылся — это не успех. Строка
        # запроса остаётся: повтор с тем же ключом повторит readback, а
        # оператор видит по ней, что попытка была.
        raise _refuse(
            REASON_READBACK_FAILED, correlation_id=correlation_id, specialist_id=specialist_id,
        )

    logger.info(
        "users.specialist_identity_link.created specialist=%s actor=%s correlation_id=%s",
        profile.pk, actor, correlation_id or "-",
    )
    return SpecialistIdentityLink(
        profile=profile, user=profile.user, proxy=proxy, request=request_row, created=True,
    )


__all__ = [
    "LINK_TARGET_ROLES",
    "SpecialistIdentityLink",
    "SpecialistIdentityLinkRefused",
    "link_specialist_identity",
]
