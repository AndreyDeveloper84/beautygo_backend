"""URL-модуль только для теста политики Sentry на настоящем SDK: ручка, которая падает.

Подключается ``@pytest.mark.urls`` в ``test_sentry_policy_live_sdk.py`` и больше нигде.
"""
from django.http import HttpRequest
from django.urls import path
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def boom(request: HttpRequest, item_id: int):
    raise RuntimeError("probe failure for the Sentry policy test")


@csrf_exempt
def boom_with_identity(request: HttpRequest):
    """Падение, чей ТЕКСТ несёт внешнюю личность и телефон (DRF-2020 C).

    Ровно та форма, что была в живом коде: `f"... got {external_user_id!r}"`.
    Ручка нужна затем, что узел обязан идти путём продукта — через настоящий
    SDK и WSGI-вход, — а не звать чистку напрямую.
    """
    identity = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
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
