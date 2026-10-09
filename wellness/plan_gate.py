"""Вторая, fail-closed линия гейта согласия и удаления у ручек движка Плана.

Решение владельца 09.10.2026 (дословно — ``AYLA_PLAN_OWNER_RULING``, раздел
«Спецификация гейта согласия/удаления»). При отзыве согласия или активной
заявке на удаление:

* сохранённый план можно просматривать;
* нельзя собирать новый план, редактировать или пересобирать его;
* нельзя подбирать услугу, переходить к записи или создавать запись;
* нельзя запускать новую обработку или пересчёт;
* использование плана не продлевает срок его хранения.

«Гейт должен быть одинаковым в боте и каталоге. Бот отвечает человеку
понятным сообщением, каталог остаётся второй fail-closed линией.»

Замер до правки: ручки ``wellness.plan_engine_api`` защищены сервисным
токеном и подтверждённой личностью клиента и не спрашивали ни о заявке на
удаление, ни о согласии — человек с живой заявкой сохранял план.

**Заявка на удаление.** Каталог знает её сам
(:func:`users.deletion_requests.deletion_block_for` — единственный читатель
состояния заявки, с учётом прокси и привязанного аккаунта) и отвечает тем
же отказом 423, что память, рекомендации и проактив.

**Согласие.** Реестр согласий живёт в боте; каталог знает только последнее
ДОСТАВЛЕННОЕ ботом состояние (``users.ConsentState``). План держит вид
:data:`PLAN_CONSENT` — согласие на хранение личных данных. Здесь закрыт
случай, который каталог может судить сам: **известный отзыв** — последнее
доставленное состояние этого вида есть и оно «отозвано».

Известный отзыв — не приговор навсегда. Бот сообщает каталогу об отзыве,
но о согласии, данном снова, сегодня не сообщает; человек, который отозвал
и вернулся, был бы закрыт здесь без срока. Поэтому отзыв побеждает, только
если он ПОЗЖЕ утверждения основания, которое бот прислал в теле вызова
(``consent: {type, document_version, granted_at}``; ``granted_at`` — время
выдачи действующего согласия). Утверждение старше отзыва — «бот проверил по
устаревшему состоянию», отказ. Время отзыва в событии бота — момент отзыва.

**Чего этот шаг НЕ закрывает.** «Согласия не было никогда» и «бот не
сообщал» каталогу неразличимы — в обоих случаях строки состояния нет. Их
закрывает следующий шаг: бот на каждом пишущем вызове присылает утверждение
основания (``consent: {type, document_version}``), и вызов без него получает
отказ. Требование не включено здесь намеренно: сначала поле начинают слать
вызывающие, потом каталог начинает его требовать — иначе окно, в котором
каждый плановый вызов отвечает отказом.

**Область.** Гейт стоит на каждом методе, который пишет или запускает
обработку (:func:`gated`), и на создании записи от шага плана. Чтение
своего плана и подписи шагов под гейт не ставятся. Архив собственного плана
открыт: в списке запретов владельца его нет — это распоряжение своим, а не
новая обработка, и срок хранения оно не продлевает. Снятие ограничения
закрыто: это ослабление сторожа.
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import wraps

from rest_framework import status

from users.consent_events import PERSONAL_DATA
from users.deletion_requests import deletion_block_for, deletion_refusal
from users.response import error_response

logger = logging.getLogger(__name__)

#: Вид согласия, под которым хранится и обрабатывается план: хранение личных
#: данных. Имя — из реестра согласий бота; сведений о здоровье план не несёт,
#: поэтому согласие на здоровье здесь не требуется.
PLAN_CONSENT = PERSONAL_DATA
CONSENT_WITHDRAWN = "withdrawn"


def known_withdrawal_at(user) -> datetime | None:
    """Момент самого позднего известного отзыва согласия на хранение — или ``None``.

    Смотрятся все строки одного человека (аккаунт и его прокси): событие
    отзыва приходит на ту оболочку, с которой человек его дал.
    """
    from users.deletion_requests import _person_rows
    from users.models import ConsentState

    return (
        ConsentState.objects.filter(user__in=_person_rows(user), consent_type=PLAN_CONSENT, granted=False)
        .order_by("-granted_at")
        .values_list("granted_at", flat=True)
        .first()
    )


def attested_granted_at(payload) -> datetime | None:
    """Время выдачи действующего согласия из утверждения основания — или ``None``.

    ``None`` — утверждения нет или оно негодно: не тот вид, пустая версия
    текста, время без часового пояса или неразборчивое. Fail-closed: негодное
    утверждение ничем не лучше отсутствующего. Значение версии не толкуется —
    у старых записей согласия бот шлёт явную метку вместо пустоты.
    """
    attestation = payload.get("consent") if hasattr(payload, "get") else None
    if not isinstance(attestation, dict) or attestation.get("type") != PLAN_CONSENT:
        return None
    version = attestation.get("document_version")
    if not isinstance(version, str) or not version.strip():
        return None
    raw = attestation.get("granted_at")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def refusal_for(user, payload=None):
    """Отказ для этого человека — или ``None``, если гейт открыт.

    Заявка на удаление раньше согласия: у неё есть номер и срок, и человек на
    любом экране должен видеть один и тот же отказ. ``payload`` — тело
    вызова; из него берётся утверждение основания.
    """
    blocked = deletion_block_for(user)
    if blocked is not None:
        return deletion_refusal(blocked)
    withdrawn_at = known_withdrawal_at(user)
    attested = attested_granted_at(payload)
    if withdrawn_at is not None and not (attested is not None and attested > withdrawn_at):
        logger.info("wellness.plan_gate.consent_refused user=%s reason=%s", user.pk, CONSENT_WITHDRAWN)
        return error_response(
            "CONSENT_REQUIRED",
            "План не собирается и не изменяется: согласие на хранение данных отозвано.",
            details={"consent_type": PLAN_CONSENT, "reason": CONSENT_WITHDRAWN},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return None


def gated(method=None, *, unless=None):
    """Метод ручки, который пишет или запускает обработку: сначала гейт.

    Гейт раньше разбора тела: человеку с заявкой на удаление не отвечают
    «тело неверно» — ему отвечают, почему обработка остановлена.

    Но ПОСЛЕ флага движка: при выключенном движке гейт молчит, и ручка
    отвечает своим «движок выключен». По этому ответу вызывающий решает
    «движка нет — работаю по-старому»; отказ гейта на его месте читался бы
    как «нет согласия» там, где функции просто нет. Fail-closed это не
    ослабляет: выключенный движок не пишет и не обрабатывает ничего.

    ``unless`` — названное исключение внутри ручки: функция от запроса,
    которая говорит «этот вызов под гейт не идёт» (архив своего плана).
    """

    def decorate(handler):
        @wraps(handler)
        def wrapper(self, request, *args, **kwargs):
            from .plan_engine import plan_engine_enabled

            if plan_engine_enabled() and (unless is None or not unless(request)):
                refusal = refusal_for(request.user, request.data)
                if refusal is not None:
                    return refusal
            return handler(self, request, *args, **kwargs)

        wrapper.plan_gate = True
        return wrapper

    return decorate(method) if method is not None else decorate


def archiving_own_plan(request) -> bool:
    """Вызов смены состояния просит ровно одно — отправить план в архив."""
    from .models import Plan

    data = request.data if isinstance(request.data, dict) else {}
    return data.get("state") == Plan.Status.ARCHIVED
