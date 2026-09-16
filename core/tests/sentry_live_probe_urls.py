"""URL-модуль только для теста политики Sentry на настоящем SDK: ручка, которая падает.

Подключается ``@pytest.mark.urls`` в ``test_sentry_policy_live_sdk.py`` и больше нигде.
"""
from django.http import HttpRequest
from django.urls import path
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def boom(request: HttpRequest, item_id: int):
    raise RuntimeError("probe failure for the Sentry policy test")


class ProbeIdentityError(RuntimeError):
    """Стоит рядом с ручкой: тест проверяет и класс в заменяющей строке."""


@csrf_exempt
def boom_with_value(request: HttpRequest, item_id: int):
    """Падает СО значением из запроса — иначе утечку нечем показать.

    DRF-2020: ``boom`` выше падает с константным текстом, и сторож на нём был бы
    зелёным при любом скруббере. Здесь значение приходит снаружи и попадает в
    сообщение исключения ровно так, как это делают девять мест
    ``users/services.py`` с ``{external_user_id!r}``.
    """

    value = request.GET.get("identity", "")
    raise ProbeIdentityError(f"external identity {value!r} is unknown")


urlpatterns = [
    path("api/v1/sentry-probe/<int:item_id>/boom/", boom, name="sentry-policy-probe-boom"),
    path(
        "api/v1/sentry-probe/<int:item_id>/boom-with-value/",
        boom_with_value,
        name="sentry-policy-probe-boom-with-value",
    ),
]
