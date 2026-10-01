"""DRF-2681 — двери биллинга мастера держат ``return_url``: только https.

``card-setup`` и ``pay-debt`` читали ``return_url`` из тела сырым, с одной
проверкой «не пусто», и отдавали его платёжному провайдеру как адрес возврата.
Слово владельца (01.10.2026): проверку ставить, только https, отказ — 400 от
двери, а не ошибка провайдера.

Пара на обе стороны у каждой двери: негодный адрес — 400 и сервис не вызван;
https — сервис вызван ровно с ним. Сервис в отказной ветке отдаёт настоящий
результат, чтобы снятая проверка читалась как ``200 != 400``.

У ``pay-debt`` адрес необязателен (нужен только без сохранённой карты):
присланный не-https отвергается, отсутствующий при сохранённой карте — нет;
«долга нет» по-прежнему отвечает раньше проверки тела.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from billing.charges import CardSetupResult, DebtPaymentResult
from billing.models import BillingInvoice

CARD_SETUP_URL = "/api/v1/internal/billing/specialists/{user_id}/card-setup/"
PAY_DEBT_URL = "/api/v1/internal/billing/specialists/{user_id}/pay-debt/"

GOOD_URL = "https://miniapp.example/master/billing"

#: Первое — ровно то, что бот слал до своей правки: ``str({"a": 1})``.
#: ``http://`` — литералом: «только https» решено словом владельца.
NOT_HTTPS = [
    "{'a': 1}",
    {"a": 1},
    123,
    "http://miniapp.example/master/billing",
    "ftp://miniapp.example/master/billing",
    "javascript:alert(1)",
    "miniapp.example/master/billing",
]


@pytest.fixture
def api(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer test-bearer")
    return client


def _card_setup_result():
    return CardSetupResult(
        subscription_id=uuid4(), invoice_id=uuid4(),
        confirmation_url="https://pay.example/confirm",
    )


def _debt_result():
    return DebtPaymentResult(
        payment_id=uuid4(), invoice_id=uuid4(),
        provider_payment_id="yk_debt_2681",
        confirmation_url="https://pay.example/debt", amount=Decimal("690.00"),
        status="pending",
    )


class TestCardSetupHoldsHttps:
    @pytest.mark.parametrize("return_url", NOT_HTTPS)
    def test_a_non_https_url_is_refused_and_the_service_is_not_called(
        self, api, specialist, return_url,
    ):
        with patch(
            "billing.internal_api.start_card_setup",
            return_value=_card_setup_result(),
        ) as setup:
            resp = api.post(
                CARD_SETUP_URL.format(user_id=specialist.user_id),
                {"tariff": "solo", "return_url": return_url},
                format="json",
            )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert setup.call_count == 0

    def test_an_https_url_reaches_the_service_verbatim(self, api, specialist):
        with patch(
            "billing.internal_api.start_card_setup",
            return_value=_card_setup_result(),
        ) as setup:
            resp = api.post(
                CARD_SETUP_URL.format(user_id=specialist.user_id),
                {"tariff": "solo", "return_url": GOOD_URL},
                format="json",
            )

        assert resp.status_code == 200, resp.json()
        assert setup.call_count == 1
        assert setup.call_args.kwargs["return_url"] == GOOD_URL


class TestPayDebtHoldsHttps:
    @pytest.fixture
    def debt_invoice(self, db, subscription):
        """An unpaid (FAILED) invoice — without it the endpoint 409s."""
        return BillingInvoice.objects.create(
            subscription=subscription,
            period_start=date(2026, 7, 11), period_end=date(2026, 8, 10),
            subscription_amount=Decimal("690.00"),
            total_amount=Decimal("690.00"),
            status=BillingInvoice.Status.FAILED,
            idempotency_key="charge:return-url-2681:1",
        )

    @pytest.fixture
    def saved_card(self, subscription):
        subscription.payment_method_id = "pm_2681"
        subscription.save(update_fields=["payment_method_id"])
        return subscription

    def _post(self, api, specialist, body):
        with patch(
            "billing.internal_api.pay_debt", return_value=_debt_result(),
        ) as pay:
            resp = api.post(
                PAY_DEBT_URL.format(user_id=specialist.user_id), body, format="json",
            )
        return resp, pay

    @pytest.mark.parametrize("return_url", NOT_HTTPS)
    def test_a_non_https_url_is_refused_and_the_service_is_not_called(
        self, api, specialist, subscription, debt_invoice, return_url,
    ):
        resp, pay = self._post(api, specialist, {"return_url": return_url})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert pay.call_count == 0

    def test_a_non_https_url_is_refused_even_with_a_saved_card(
        self, api, specialist, saved_card, debt_invoice,
    ):
        resp, pay = self._post(
            api, specialist, {"return_url": "http://miniapp.example/debt"},
        )

        assert resp.status_code == 400
        assert pay.call_count == 0

    def test_an_https_url_reaches_the_service_verbatim(
        self, api, specialist, subscription, debt_invoice,
    ):
        resp, pay = self._post(api, specialist, {"return_url": GOOD_URL})

        assert resp.status_code == 200, resp.json()
        assert pay.call_count == 1
        assert pay.call_args.kwargs["return_url"] == GOOD_URL

    def test_no_url_is_still_fine_with_a_saved_card(
        self, api, specialist, saved_card, debt_invoice,
    ):
        resp, pay = self._post(api, specialist, {})

        assert resp.status_code == 200, resp.json()
        assert pay.call_count == 1
        assert pay.call_args.kwargs["return_url"] == ""

    def test_no_debt_still_answers_before_the_body_is_read(
        self, api, specialist, subscription,
    ):
        resp, pay = self._post(
            api, specialist, {"return_url": "http://miniapp.example/debt"},
        )

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "NO_DEBT"
        assert pay.call_count == 0
