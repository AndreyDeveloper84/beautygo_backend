"""URL-модуль только для теста политики Sentry на настоящем SDK: ручка, которая падает.

Подключается ``@pytest.mark.urls`` в ``test_sentry_policy_live_sdk.py`` и больше нигде.
"""
import logging

import sentry_sdk
from django.http import HttpRequest
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


@csrf_exempt
def boom(request: HttpRequest, item_id: int):
    raise RuntimeError("probe failure for the Sentry policy test")


@csrf_exempt
def boom_with_identity(request: HttpRequest):
    """Падение, чей текст несёт личность и телефон — и ещё ДВА носителя.

    Ровно та форма, что была в живом коде: `f"... got {external_user_id!r}"`.
    Ручка нужна затем, что узел обязан идти путём продукта — через настоящий
    SDK и WSGI-вход, — а не звать чистку напрямую.

    Личность пишется в ТРИ места, и это не щедрость (DRF-2020 C, найдено
    ревью). С одним носителем «личности нет ни в одном поле» было утверждением
    про ОДНО поле: подмена, чистящая только `exception[].value`, проходила
    зелёной — та самая узкая починка, против которой написана рекурсия.

    Замером установлено, чем именно эти три различаются, и это важнее числа:

    * `exception[].value` — приходит в событие СЫРЫМ;
    * `extra.probe_identity_note` (через `set_extra`) — тоже СЫРЫМ, и это
      второй носитель, ради которого ручка и правилась: он структурно другой,
      поэтому узкая чистка одного `exception` на нём краснеет;
    * хлебная крошка из журнала — приходит УЖЕ ЧИСТОЙ (`[IDENTITY]`). Я
      ожидал обратного: фильтр ПДн стоит на обработчике `console`, а крошки
      Sentry собирает своим. Но фильтр правит саму запись журнала, и Sentry
      видит её после фильтра. Значит крошки закрыты нижним слоем, а не
      рекурсией — и доказать рекурсию крошкой НЕЛЬЗЯ. Строка оставлена как
      утверждение об этом слое, а не как носитель.
    """
    identity = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
    logger.info("probe resolving %s before the failure", identity)
    sentry_sdk.set_extra("probe_identity_note", f"seen {identity} on this request")
    raise RuntimeError(
        f"external_user_id must match '<source>:<id>', got {identity!r}; "
        f"callback phone +79991234567"
    )


urlpatterns = [
    path("api/v1/sentry-probe/<int:item_id>/boom/", boom, name="sentry-policy-probe-boom"),
    path(
        "api/v1/sentry-probe/identity/boom/",
        boom_with_identity,
        name="sentry-policy-probe-identity",
    ),
]
