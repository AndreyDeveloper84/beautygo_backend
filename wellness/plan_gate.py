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

**Чего этот шаг НЕ закрывает.** «Согласия не было никогда» и «бот не
сообщал» каталогу неразличимы — в обоих случаях строки состояния нет. Их
закрывает следующий шаг: бот на каждом пишущем вызове присылает утверждение
основания (``consent: {type, document_version}``), и вызов без него получает
отказ. Требование не включено здесь намеренно: сначала поле начинают слать
вызывающие, потом каталог начинает его требовать — иначе окно, в котором
каждый плановый вызов отвечает отказом.

**Область.** Гейт стоит на каждом методе, который пишет или запускает
обработку (:func:`gated`), и на создании записи от шага плана. Чтение
своего плана и подписи шагов под гейт не ставятся.
"""

from __future__ import annotations

import logging
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


def consent_is_known_withdrawn(user) -> bool:
    """Последнее доставленное ботом состояние согласия на хранение — отзыв.

    Смотрятся все строки одного человека (аккаунт и его прокси): событие
    отзыва приходит на ту оболочку, с которой человек его дал.
    """
    from users.deletion_requests import _person_rows
    from users.models import ConsentState

    return ConsentState.objects.filter(
        user__in=_person_rows(user), consent_type=PLAN_CONSENT, granted=False,
    ).exists()


def refusal_for(user):
    """Отказ для этого человека — или ``None``, если гейт открыт.

    Заявка на удаление раньше согласия: у неё есть номер и срок, и человек на
    любом экране должен видеть один и тот же отказ.
    """
    blocked = deletion_block_for(user)
    if blocked is not None:
        return deletion_refusal(blocked)
    if consent_is_known_withdrawn(user):
        logger.info("wellness.plan_gate.consent_refused user=%s reason=%s", user.pk, CONSENT_WITHDRAWN)
        return error_response(
            "CONSENT_REQUIRED",
            "План не собирается и не изменяется: согласие на хранение данных отозвано.",
            details={"consent_type": PLAN_CONSENT, "reason": CONSENT_WITHDRAWN},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return None


def gated(method):
    """Метод ручки, который пишет или запускает обработку: сначала гейт.

    Гейт раньше разбора тела и раньше проверки флага движка: человеку с
    заявкой на удаление не отвечают ни «тело неверно», ни «движок выключен»
    — ему отвечают, почему обработка остановлена.
    """

    @wraps(method)
    def wrapper(self, request, *args, **kwargs):
        refusal = refusal_for(request.user)
        if refusal is not None:
            return refusal
        return method(self, request, *args, **kwargs)

    wrapper.plan_gate = True
    return wrapper
