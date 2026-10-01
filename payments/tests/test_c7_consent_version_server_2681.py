"""DRF-2681 — версию согласия на привязку карты ставит сервер.

``UserPaymentMethod.consent_version`` — запись о том, на что согласился
человек, основание хранить его платёжные данные. До этой правки каталог писал
туда то, что прислал клиент: любую непустую строку до 64 символов — ``"v999"``,
``"{'a': 1}"``. Слово владельца (01.10.2026): сервер ставит версию, клиенту не
верит — как уже сделано для оферты мастера (``billing.charges._offer_version``).

Узлы смотрят не код ответа двери, а то, что ушло провайдеру и что легло в
``UserPaymentMethod``: дверь отвечает 200 в обоих случаях, различается запись.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from payments.models import UserPaymentMethod
from payments.services import YooKassaService
from payments.tests.test_c7_client_payments import (  # noqa: F401
    WEBHOOK_URL,
    _api,
    _card_setup_url,
    _token,
    customer,
)

pytestmark = pytest.mark.django_db

#: Литералом, а не из кода: смена умолчания обязана покраснеть здесь.
SERVER_DEFAULT = "offer-client-cards-0.0-todo-legal"
RETURN_URL = "https://miniapp.example/cards/done"

#: То, чему каталог больше не верит. ``"{'a': 1}"`` — ровно то, что бот слал
#: до своей правки; ``"v999"`` — годная по типу версия, которой не существует.
CLIENT_SENT = ["v999", "{'a': 1}", "card-consent-v1"]


def _binding_result():
    return {
        "provider_payment_id": "yk_bind_2681",
        "confirmation_url": "https://yookassa.ru/bind/2681",
        "status": "pending",
    }


def _setup(customer, consent_version, svc):  # noqa: F811
    with patch("payments.views._get_yookassa", return_value=svc):
        return _api().post(
            _card_setup_url(customer.id),
            {"consent_version": consent_version, "return_url": RETURN_URL},
            format="json",
        )


class _FakePaymentSdk:
    """Стоит на месте ``yookassa.Payment``: запоминает, что ушло провайдеру."""

    def __init__(self):
        self.sent: list[dict] = []

    def create(self, payload, idempotency_key):
        self.sent.append(payload)
        return SimpleNamespace(
            id="yk_bind_2681",
            status="pending",
            confirmation=SimpleNamespace(
                confirmation_url="https://yookassa.ru/bind/2681",
            ),
        )


def _mismatch_calls(log) -> int:
    # Логгер ``payments`` не отдаёт записи выше (propagate=False) — ``caplog``
    # их не видит и узел на нём не может упасть; смотрим вызовы логгера модуля.
    return sum(
        1 for call in log.warning.call_args_list
        if str(call.args[0]).startswith("card_binding.consent_version_mismatch")
    )


def _real_service(sdk):
    svc = YooKassaService.__new__(YooKassaService)  # без ключей провайдера
    svc._payment_cls = sdk
    return svc


class TestTheServerStampsTheVersion:
    @pytest.mark.parametrize("sent", CLIENT_SENT)
    def test_the_service_gets_the_server_version_not_the_sent_one(
        self, customer, sent  # noqa: F811
    ):
        svc = MagicMock()
        svc.create_card_binding.return_value = _binding_result()

        r = _setup(customer, sent, svc)

        assert r.status_code == 200, r.data
        assert svc.create_card_binding.call_args.kwargs["consent_version"] == SERVER_DEFAULT

    def test_the_version_comes_from_the_setting(self, customer, settings):  # noqa: F811
        settings.CLIENT_CARD_CONSENT_VERSION = "offer-client-cards-1.0"
        svc = MagicMock()
        svc.create_card_binding.return_value = _binding_result()

        r = _setup(customer, "v999", svc)

        assert r.status_code == 200, r.data
        assert (
            svc.create_card_binding.call_args.kwargs["consent_version"]
            == "offer-client-cards-1.0"
        )

    def test_a_sent_version_that_differs_is_logged_not_trusted(self, customer):  # noqa: F811
        svc = MagicMock()
        svc.create_card_binding.return_value = _binding_result()

        with patch("payments.views.logger") as log:
            _setup(customer, "v999", svc)

        assert _mismatch_calls(log) == 1
        assert "v999" not in str(log.mock_calls)

    def test_the_server_version_sent_back_is_not_a_mismatch(self, customer):  # noqa: F811
        svc = MagicMock()
        svc.create_card_binding.return_value = _binding_result()

        with patch("payments.views.logger") as log:
            r = _setup(customer, SERVER_DEFAULT, svc)

        assert r.status_code == 200, r.data
        assert _mismatch_calls(log) == 0


class TestTheRecordHoldsTheServerVersion:
    """Дверь → метаданные провайдера → вебхук → ``UserPaymentMethod``."""

    @pytest.mark.parametrize("sent", CLIENT_SENT)
    def test_what_the_client_sent_never_reaches_the_record(
        self, customer, sent  # noqa: F811
    ):
        sdk = _FakePaymentSdk()
        r = _setup(customer, sent, _real_service(sdk))
        assert r.status_code == 200, r.data
        (payload,) = sdk.sent
        metadata = payload["metadata"]
        assert metadata["consent_version"] == SERVER_DEFAULT

        webhook_svc = MagicMock()
        webhook_svc.get_payment_info.return_value = {
            "provider_payment_id": "yk_bind_2681",
            "status": "succeeded",
            "paid": True,
            "metadata": metadata,
            "payment_method": {
                "id": "pm_2681", "saved": True, "last4": "4242", "brand": "Visa",
            },
        }
        api = APIClient()
        api.defaults["HTTP_X_APP_TYPE"] = "client"
        with patch("payments.views._get_yookassa", return_value=webhook_svc):
            w = api.post(
                WEBHOOK_URL,
                {"event": "payment.succeeded", "object": {"id": "yk_bind_2681"}},
                format="json",
            )
        assert w.status_code == 200

        card = UserPaymentMethod.objects.get()
        assert card.user_id == customer.id
        assert card.consent_version == SERVER_DEFAULT
