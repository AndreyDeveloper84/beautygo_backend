"""URL-модуль только для теста политики Sentry на настоящем SDK: ручка, которая падает.

Подключается ``@pytest.mark.urls`` в ``test_sentry_policy_live_sdk.py`` и больше нигде.
"""
from django.http import HttpRequest
from django.urls import path
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def boom(request: HttpRequest, item_id: int):
    raise RuntimeError("probe failure for the Sentry policy test")


urlpatterns = [
    path("api/v1/sentry-probe/<int:item_id>/boom/", boom, name="sentry-policy-probe-boom"),
]
