"""DRF-2652 — отправка кода говорит, чем она кончилась; ручки отвечают как раньше.

До листа ``OTPService.send_otp`` возвращал ``None`` и результат отправителя не
читал: человеку отвечали «код отправлен» и при отказе провайдера, и при
выключенной отправке. После DRF-2643 отправитель говорит правду — OTP её
по-прежнему не слушал.

Что этот лист делает и чего НЕ делает.

* Делает: три исхода вместо молчания (``sent`` / ``dev_code`` / ``not_sent``) и
  след в журнале у несостоявшейся отправки — без номера и без кода.
* НЕ делает: ответы четырёх ручек не меняются. Что отвечать человеку, когда
  код не ушёл, — новые слова на экране, и их решает владелец. Вторая половина
  узлов ниже держит именно это: при отказе провайдера ручки отвечают теми же
  словами, что и при удаче.

Тройка обязана различаться с обеих сторон: склеить ``sent`` с ``not_sent`` —
вернуть дефект; склеить ``not_sent`` с ``dev_code`` — сломать вход в
разработке, где код известен заранее и «не отправлено» — законный исход.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import OTPCode, User
from users.services import OtpSendOutcome, OTPService

LOGGER = "users.services"
NOT_DELIVERED = "otp.not_delivered"


def _provider(status_code: int):
    """Провайдер отвечает кодом ``status_code``: 100 — принял, иное — отказал."""
    response = MagicMock()
    response.json.return_value = {"status_code": status_code, "status_text": "refused"}
    return patch("users.sms.requests.get", return_value=response)


def _provider_accepts():
    return _provider(100)


def _provider_refuses():
    return _provider(201)


@pytest.fixture
def sending_on(settings):
    settings.DEBUG = False
    settings.SMS_ENABLED = True
    settings.SMS_RU_API_ID = "test-api-id"  # pragma: allowlist secret


@pytest.fixture
def not_delivered(caplog):
    """Строки ``otp.not_delivered`` за время узла.

    Логгер ``users`` объявлен ``propagate=False`` (settings/base.py, LOGGING),
    поэтому корневой handler ``caplog`` записей ``users.services`` не видит —
    без подвески «строк нет» было бы верно всегда, и узлы на отсутствие следа
    проходили бы вхолостую. Тот же приём, что в
    ``test_sms_credentials_never_in_logs_2020.py``.
    """
    service_logger = logging.getLogger(LOGGER)
    service_logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=LOGGER)

    def _lines() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == LOGGER and r.getMessage().startswith(NOT_DELIVERED)
        ]

    yield _lines
    service_logger.removeHandler(caplog.handler)


# --------------------------------------------------------------------------- #
# 1. Тройка на уровне сервиса                                                  #
# --------------------------------------------------------------------------- #
class TestTheOutcomeWordsAreTheDecidedOnes:
    def test_three_outcomes_by_their_literal_values(self):
        assert {o.value for o in OtpSendOutcome} == {"sent", "dev_code", "not_sent"}


@pytest.mark.django_db
class TestTheServiceSaysHowTheSendingEnded:
    def test_accepted_by_the_provider_is_sent(self, sending_on, not_delivered):
        with _provider_accepts() as get:
            outcome = OTPService().send_otp("+79990002652")

        assert outcome is OtpSendOutcome.SENT
        get.assert_called_once()
        assert not_delivered() == []

    def test_refused_by_the_provider_is_not_sent_and_leaves_a_trace(
        self, sending_on, not_delivered
    ):
        phone = "+79990002653"
        with _provider_refuses() as get:
            outcome = OTPService().send_otp(phone)

        assert outcome is OtpSendOutcome.NOT_SENT
        get.assert_called_once()
        assert not_delivered() == ["otp.not_delivered reason=send_failed"]
        # код человеку выдан, а до него не дошёл — именно этот случай и считаем
        code = OTPCode.objects.get(phone=phone).code
        assert code != ""
        for line in not_delivered():
            assert phone not in line and phone.lstrip("+") not in line
            assert code not in line

    def test_a_provider_that_is_not_configured_is_not_sent(
        self, settings, sending_on, not_delivered
    ):
        settings.SMS_RU_API_ID = ""
        with _provider_accepts() as get:
            outcome = OTPService().send_otp("+79990002654")

        assert outcome is OtpSendOutcome.NOT_SENT
        get.assert_not_called()
        assert not_delivered() == ["otp.not_delivered reason=send_failed"]

    def test_sending_switched_off_outside_development_is_not_sent(
        self, settings, not_delivered
    ):
        """Выключено, а режим не разработка: выдан настоящий код, и он никуда не ушёл."""
        settings.DEBUG = False
        settings.SMS_ENABLED = False
        phone = "+79990002655"
        with _provider_accepts() as get:
            outcome = OTPService().send_otp(phone)

        assert outcome is OtpSendOutcome.NOT_SENT
        get.assert_not_called()
        assert not_delivered() == ["otp.not_delivered reason=sending_disabled"]
        assert OTPCode.objects.get(phone=phone).code != settings.OTP_DEBUG_CODE

    def test_development_mode_is_its_own_outcome_and_not_a_failure(
        self, settings, not_delivered
    ):
        """Ловушка листа: «не отправлено» в разработке — успех, код известен заранее."""
        settings.DEBUG = True
        settings.SMS_ENABLED = False
        phone = "+79990002656"
        with _provider_accepts() as get:
            outcome = OTPService().send_otp(phone)

        assert outcome is OtpSendOutcome.DEV_CODE
        assert outcome is not OtpSendOutcome.NOT_SENT
        get.assert_not_called()
        assert OTPCode.objects.get(phone=phone).code == settings.OTP_DEBUG_CODE
        assert not_delivered() == []


# --------------------------------------------------------------------------- #
# 2. Ответы четырёх ручек НЕ изменились                                        #
# --------------------------------------------------------------------------- #
@pytest.fixture
def api() -> APIClient:
    return APIClient(headers={"X-App-Type": "client"})


def _existing_user(phone: str) -> User:
    return User.objects.create_user(
        username=f"user_{phone.lstrip('+')}", phone=phone, role="client", password=None
    )


#: Провайдер принял и провайдер отказал — ручка обязана ответить одинаково,
#: пока владелец не решил, какие слова человек видит при неотправленном коде.
_PROVIDERS = pytest.mark.parametrize(
    "provider", [_provider_accepts, _provider_refuses], ids=["accepted", "refused"]
)


@pytest.mark.django_db
@pytest.mark.usefixtures("sending_on")
class TestTheFourEndpointsAnswerAsBefore:
    @_PROVIDERS
    def test_register(self, api, provider):
        phone = "+79990012652"
        with provider():
            resp = api.post(reverse("register"), {"phone": phone}, format="json")

        assert resp.status_code == 201
        assert resp.json() == {"data": {"phone": phone, "message": "OTP sent"}}

    @_PROVIDERS
    def test_login(self, api, provider):
        phone = "+79990022652"
        _existing_user(phone)
        with provider():
            resp = api.post(reverse("login"), {"phone": phone}, format="json")

        assert resp.status_code == 200
        assert resp.json() == {"data": {"message": "OTP sent"}}

    @_PROVIDERS
    def test_unified_send_otp(self, api, settings, provider):
        phone = "+79990032652"
        with provider():
            resp = api.post(reverse("send-otp"), {"phone": phone}, format="json")

        assert resp.status_code == 200
        assert resp.json() == {
            "data": {
                "expires_in": settings.OTP_EXPIRY_MINUTES * 60,
                "retry_after": settings.OTP_RATE_LIMIT_SECONDS,
                "is_new_user": True,
            }
        }

    @_PROVIDERS
    def test_resend_code(self, api, provider):
        phone = "+79990042652"
        _existing_user(phone)
        with provider():
            resp = api.post(reverse("send-code"), {"phone": phone}, format="json")

        assert resp.status_code == 200
        assert resp.json() == {"data": {"message": "OTP sent"}}

    def test_the_refused_case_really_was_a_refusal(self, api, not_delivered):
        """Положительный контроль: «тот же ответ» получен именно при несостоявшейся отправке."""
        phone = "+79990052652"
        _existing_user(phone)
        with _provider_refuses() as get:
            resp = api.post(reverse("send-code"), {"phone": phone}, format="json")

        assert resp.status_code == 200
        get.assert_called_once()
        assert not_delivered() == ["otp.not_delivered reason=send_failed"]
