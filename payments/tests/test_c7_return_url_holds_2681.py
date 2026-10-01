"""DRF-2681 — «закрыто ниже по пути» держится: C7 отвергает негодный return_url.

Бот (ai-bot-platform DRF-2681) теперь сам отказывает нестроковому
``return_url`` в дверях C7 Mini App; до его правки словарь из тела уходил сюда
текстом ``"{'a': 1}"``. Каталог держал и держит это ``URLField`` у
``_InternalPaymentCreateSerializer`` и ``_InternalCardSetupSerializer``.
Защита двойная, и сигнала о потере одной половины нет: эти узлы ничего не
меняют в поведении — они фиксируют существующее закрытие и покраснеют, если
``URLField`` ослабят.

Пара на обе стороны, иначе узел не различает: негодный адрес — отказ и
провайдер не вызван; годный — провайдер вызван ровно с ним.

Третья внутренняя дверь, ``payments/internal/<id>/retry/``, — в
``test_internal_retry_return_url_holds_2681.py`` (свои фикстуры).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from payments.tests.test_c7_client_payments import (  # noqa: F401
    _api,
    _card_setup_url,
    _mock_create_result,
    _payment_url,
    _token,
    appointment,
    customer,
    service,
    specialist,
)

pytestmark = pytest.mark.django_db

#: Ровно то, что бот слал до своей правки: ``str({"a": 1})``.
NOT_A_URL = "{'a': 1}"
GOOD_URL = "https://miniapp.example/done"


def _payment(appointment, customer, return_url, svc):  # noqa: F811
    with patch("payments.views._get_yookassa", return_value=svc):
        return _api().post(
            _payment_url(appointment.id),
            {"client_id": str(customer.id), "return_url": return_url},
            format="json",
        )


def _card_setup(customer, return_url, svc):  # noqa: F811
    with patch("payments.views._get_yookassa", return_value=svc):
        return _api().post(
            _card_setup_url(customer.id),
            {"consent_version": "card-consent-v1", "return_url": return_url},
            format="json",
        )


class TestPaymentCreateHoldsTheUrl:
    def test_a_non_url_is_refused_and_the_provider_is_not_called(
        self, customer, appointment  # noqa: F811
    ):
        svc = MagicMock()
        svc.create_payment.return_value = _mock_create_result()

        r = _payment(appointment, customer, NOT_A_URL, svc)

        assert r.status_code == 400
        assert "return_url" in str(r.data)
        assert svc.create_payment.call_count == 0

    def test_a_url_reaches_the_provider_verbatim(self, customer, appointment):  # noqa: F811
        svc = MagicMock()
        svc.create_payment.return_value = _mock_create_result()

        r = _payment(appointment, customer, GOOD_URL, svc)

        assert r.status_code == 200, r.data
        assert svc.create_payment.call_count == 1
        assert svc.create_payment.call_args.kwargs["return_url"] == GOOD_URL


def _mock_binding_result():
    return {
        "provider_payment_id": "yk_bind_2681",
        "confirmation_url": "https://yookassa.ru/bind/2681",
        "status": "pending",
    }


class TestCardSetupHoldsTheUrl:
    def test_a_non_url_is_refused_and_the_provider_is_not_called(self, customer):  # noqa: F811
        svc = MagicMock()
        # Настоящий ответ и в отказной ветке: если ``URLField`` ослабят, дверь
        # дойдёт до провайдера, и голый ``MagicMock`` в теле ответа подвесит
        # JSON-кодировщик — шард умрёт по таймауту вместо ``200 != 400``.
        svc.create_card_binding.return_value = _mock_binding_result()

        r = _card_setup(customer, NOT_A_URL, svc)

        assert r.status_code == 400
        assert "return_url" in str(r.data)
        assert svc.create_card_binding.call_count == 0

    def test_a_url_reaches_the_provider_verbatim(self, customer):  # noqa: F811
        svc = MagicMock()
        svc.create_card_binding.return_value = _mock_binding_result()

        r = _card_setup(customer, GOOD_URL, svc)

        assert r.status_code == 200, r.data
        assert svc.create_card_binding.call_args.kwargs["return_url"] == GOOD_URL
