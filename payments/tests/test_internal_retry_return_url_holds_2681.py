"""DRF-2681 — ``payments/internal/<id>/retry/`` отвергает негодный return_url.

Третья внутренняя дверь, несущая ``return_url`` от бота: держит её
``URLField`` у ``InternalPaymentRetrySerializer``. Поведение не меняется —
узлы фиксируют существующее закрытие и покраснеют, если ``URLField`` ослабят.
Зачем и почему пара — в ``test_c7_return_url_holds_2681.py``.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from payments.models import Payment
from payments.tests.test_internal_payment_retry_endpoint import (  # noqa: F401
    VALID_TOKEN,
    _api,
    _url,
    appointment,
    category,
    client_user,
    external_user_id,
    failed_payment,
    service,
    specialist,
    specialist_user,
)

pytestmark = pytest.mark.django_db

#: Ровно то, что бот слал до своей правки: ``str({"a": 1})``.
NOT_A_URL = "{'a': 1}"
GOOD_URL = "https://miniapp.example/done"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


def _retry(failed_payment, client_user, return_url, yk_cls):  # noqa: F811
    yk_cls.return_value.create_payment.return_value = {
        "specialist_income": Decimal("1380.00"),
        "platform_fee": Decimal("120.00"),
        "provider_payment_id": "yk-internal-retry-2681",
        "confirmation_url": "https://yookassa/new/internal-retry-2681",
    }
    return _api().post(
        _url(failed_payment.id),
        {"client_id": str(client_user.id), "return_url": return_url},
        format="json",
    )


class TestInternalRetryHoldsTheUrl:
    def test_a_non_url_is_refused_and_the_provider_is_not_called(
        self, client_user, failed_payment  # noqa: F811
    ):
        before = Payment.objects.count()

        with patch("payments.services.YooKassaService") as yk_cls:
            r = _retry(failed_payment, client_user, NOT_A_URL, yk_cls)

        assert r.status_code == 400
        assert "return_url" in str(r.data)
        assert yk_cls.return_value.create_payment.call_count == 0
        assert Payment.objects.count() == before

    def test_a_url_reaches_the_provider_verbatim(
        self, client_user, failed_payment  # noqa: F811
    ):
        with patch("payments.services.YooKassaService") as yk_cls:
            r = _retry(failed_payment, client_user, GOOD_URL, yk_cls)

        assert r.status_code == 201, r.data
        create_payment = yk_cls.return_value.create_payment
        assert create_payment.call_count == 1
        assert create_payment.call_args.kwargs["return_url"] == GOOD_URL
