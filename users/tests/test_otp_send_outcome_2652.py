"""DRF-2652 — «код отправлен» говорится только тогда, когда код отправлен.

До листа ``OTPService.send_otp`` возвращал ``None`` и результат отправителя не
читал: человеку отвечали «код отправлен» и при отказе провайдера, и при
выключенной отправке. Человек ждал кода, который не придёт, и повторял
попытку, которая снова «удавалась».

Решение владельца 02.10.2026: успех — только при подтверждённом результате
провайдера; при установленном отказе — понятная ошибка «Не удалось отправить
код, попробуйте ещё раз»; режим разработки отказом не считать.

Исходов три, и тройка обязана различаться с обеих сторон: склеить ``sent`` с
``not_sent`` — вернуть дефект; склеить ``not_sent`` с ``dev_code`` — сломать
вход в разработке, где код известен заранее.

Проверяется то, что видит ВЫЗЫВАЮЩИЙ, на всех четырёх поверхностях:
регистрация, вход, единая авторизация по телефону, повторная отправка.
"""
from __future__ import annotations

import logging
import re
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from core.errors import ErrorCode
from users.models import OTPCode, User
from users.services import OtpSendOutcome, OTPNotSentError, OTPService, RateLimitError

LOGGER = "users.services"
NOT_DELIVERED = "otp.not_delivered"

#: Слова владельца — дословно. Литералом, чтобы их смена не прошла молча.
OWNER_WORDS = "Не удалось отправить код, попробуйте ещё раз"


def _provider(status_code: int):
    """Провайдер отвечает кодом ``status_code``: 100 — принял, иное — отказал."""
    response = MagicMock()
    response.json.return_value = {"status_code": status_code, "status_text": "refused"}
    return patch("users.sms.requests.get", return_value=response)


def _provider_accepts():
    return _provider(100)


def _provider_refuses():
    return _provider(201)


def _code_given_to_the_provider(get) -> str:
    """Код, который ушёл бы в SMS, — из текста сообщения, переданного провайдеру."""
    message = get.call_args.kwargs["params"]["msg"]
    found = re.search(r"\d{4,}", message)
    assert found is not None, "в сообщении провайдеру нет кода"
    return found.group(0)


@pytest.fixture
def sending_on(settings):
    settings.DEBUG = False
    settings.SMS_ENABLED = True
    settings.SMS_RU_API_ID = "test-api-id"  # pragma: allowlist secret


@pytest.fixture
def development_mode(settings):
    settings.DEBUG = True
    settings.SMS_ENABLED = False


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
class TestTheDecidedWords:
    def test_three_outcomes_by_their_literal_values(self):
        assert {o.value for o in OtpSendOutcome} == {"sent", "dev_code", "not_sent"}

    def test_the_refusal_is_a_registered_code_with_the_owners_words(self):
        error = OTPNotSentError()

        assert error.code == "OTP_NOT_SENT" == ErrorCode.OTP_NOT_SENT.value
        assert error.status_code == 503
        assert error.message == OWNER_WORDS


@pytest.mark.django_db
class TestTheServiceSaysHowTheSendingEnded:
    def test_accepted_by_the_provider_is_sent_and_the_code_is_kept(
        self, sending_on, not_delivered
    ):
        phone = "+79990002652"
        with _provider_accepts() as get:
            outcome = OTPService().send_otp(phone)

        assert outcome is OtpSendOutcome.SENT
        get.assert_called_once()
        assert OTPCode.objects.get(phone=phone).code == _code_given_to_the_provider(get)
        assert not_delivered() == []

    def test_refused_by_the_provider_is_not_sent_leaves_a_trace_and_no_code(
        self, sending_on, not_delivered
    ):
        phone = "+79990002653"
        with _provider_refuses() as get:
            outcome = OTPService().send_otp(phone)

        assert outcome is OtpSendOutcome.NOT_SENT
        get.assert_called_once()
        assert not_delivered() == ["otp.not_delivered reason=send_failed"]
        # в следе нет ни номера, ни кода, который человек так и не получил
        code = _code_given_to_the_provider(get)
        for line in not_delivered():
            assert phone not in line and phone.lstrip("+") not in line
            assert code not in line
        # и самого кода в базе не осталось: войти по нему нельзя
        assert OTPCode.objects.filter(phone=phone).count() == 0

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
        """Выключено, а режим не разработка: настоящий код никуда не ушёл."""
        settings.DEBUG = False
        settings.SMS_ENABLED = False
        with _provider_accepts() as get:
            outcome = OTPService().send_otp("+79990002655")

        assert outcome is OtpSendOutcome.NOT_SENT
        get.assert_not_called()
        assert not_delivered() == ["otp.not_delivered reason=sending_disabled"]

    def test_development_mode_is_its_own_outcome_and_not_a_failure(
        self, settings, development_mode, not_delivered
    ):
        """Ловушка листа: «не отправлено» в разработке — успех, код известен заранее."""
        phone = "+79990002656"
        with _provider_accepts() as get:
            outcome = OTPService().send_otp_or_fail(phone)

        assert outcome is OtpSendOutcome.DEV_CODE
        assert outcome is not OtpSendOutcome.NOT_SENT
        get.assert_not_called()
        assert OTPCode.objects.get(phone=phone).code == settings.OTP_DEBUG_CODE
        assert not_delivered() == []

    def test_the_failing_entry_point_raises_only_on_not_sent(self, sending_on):
        with _provider_accepts():
            assert OTPService().send_otp_or_fail("+79990002657") is OtpSendOutcome.SENT
        with _provider_refuses(), pytest.raises(OTPNotSentError):
            OTPService().send_otp_or_fail("+79990002658")


@pytest.mark.django_db
class TestTryAgainIsTrue:
    """Человеку сказали «попробуйте ещё раз» — значит, повтор обязан пройти."""

    def test_an_undelivered_code_does_not_hold_the_resend_limit(self, sending_on):
        phone = "+79990002659"
        with _provider_refuses():
            assert OTPService().send_otp(phone) is OtpSendOutcome.NOT_SENT
        with _provider_accepts():
            assert OTPService().send_otp(phone) is OtpSendOutcome.SENT

    def test_a_delivered_code_still_holds_the_resend_limit(self, sending_on):
        """Пара: лимит на месте там, где SMS действительно ушла."""
        phone = "+79990002660"
        with _provider_accepts():
            assert OTPService().send_otp(phone) is OtpSendOutcome.SENT
        with _provider_accepts(), pytest.raises(RateLimitError):
            OTPService().send_otp(phone)


# --------------------------------------------------------------------------- #
# 2. Четыре поверхности: что видит человек                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def api() -> APIClient:
    return APIClient(headers={"X-App-Type": "client"})


def _existing_user(phone: str) -> User:
    return User.objects.create_user(
        username=f"user_{phone.lstrip('+')}", phone=phone, role="client", password=None
    )


def _refusal(resp) -> dict:
    """Отказ «код не отправлен» — статус и тело целиком."""
    assert resp.status_code == 503
    return resp.json()


_REFUSAL_BODY = {"error": {"code": "OTP_NOT_SENT", "message": OWNER_WORDS}}


@pytest.mark.django_db
@pytest.mark.usefixtures("sending_on")
class TestSuccessIsSaidOnlyWhenTheProviderConfirmed:
    def test_register(self, api):
        phone = "+79990012652"
        with _provider_accepts():
            resp = api.post(reverse("register"), {"phone": phone}, format="json")

        assert resp.status_code == 201
        assert resp.json() == {"data": {"phone": phone, "message": "OTP sent"}}

    def test_login(self, api):
        phone = "+79990022652"
        _existing_user(phone)
        with _provider_accepts():
            resp = api.post(reverse("login"), {"phone": phone}, format="json")

        assert resp.status_code == 200
        assert resp.json() == {"data": {"message": "OTP sent"}}

    def test_unified_send_otp(self, api, settings):
        with _provider_accepts():
            resp = api.post(reverse("send-otp"), {"phone": "+79990032652"}, format="json")

        assert resp.status_code == 200
        assert resp.json() == {
            "data": {
                "expires_in": settings.OTP_EXPIRY_MINUTES * 60,
                "retry_after": settings.OTP_RATE_LIMIT_SECONDS,
                "is_new_user": True,
            }
        }

    def test_resend_code(self, api):
        phone = "+79990042652"
        _existing_user(phone)
        with _provider_accepts():
            resp = api.post(reverse("send-code"), {"phone": phone}, format="json")

        assert resp.status_code == 200
        assert resp.json() == {"data": {"message": "OTP sent"}}


@pytest.mark.django_db
@pytest.mark.usefixtures("sending_on")
class TestARefusalIsSaidAsARefusal:
    """При ``not_sent`` ни одна из четырёх поверхностей не говорит «код отправлен»."""

    def test_register(self, api):
        phone = "+79990112652"
        with _provider_refuses() as get:
            resp = api.post(reverse("register"), {"phone": phone}, format="json")

        get.assert_called_once()
        assert _refusal(resp) == _REFUSAL_BODY

    def test_login(self, api):
        phone = "+79990122652"
        _existing_user(phone)
        with _provider_refuses() as get:
            resp = api.post(reverse("login"), {"phone": phone}, format="json")

        get.assert_called_once()
        assert _refusal(resp) == _REFUSAL_BODY

    def test_unified_send_otp_for_a_new_person(self, api):
        with _provider_refuses() as get:
            resp = api.post(reverse("send-otp"), {"phone": "+79990132652"}, format="json")

        get.assert_called_once()
        assert _refusal(resp) == _REFUSAL_BODY

    def test_unified_send_otp_for_a_known_person(self, api):
        phone = "+79990142652"
        _existing_user(phone)
        with _provider_refuses() as get:
            resp = api.post(reverse("send-otp"), {"phone": phone}, format="json")

        get.assert_called_once()
        assert _refusal(resp) == _REFUSAL_BODY

    def test_the_request_otp_alias_of_the_unified_endpoint(self, api):
        """``request-otp`` — второй адрес той же ручки; человек приходит и по нему."""
        with _provider_refuses() as get:
            resp = api.post(reverse("request-otp"), {"phone": "+79990172652"}, format="json")

        get.assert_called_once()
        assert _refusal(resp) == _REFUSAL_BODY

    def test_resend_code(self, api):
        phone = "+79990152652"
        _existing_user(phone)
        with _provider_refuses() as get:
            resp = api.post(reverse("send-code"), {"phone": phone}, format="json")

        get.assert_called_once()
        assert _refusal(resp) == _REFUSAL_BODY

    def test_the_refusal_is_counted(self, api, not_delivered):
        phone = "+79990162652"
        _existing_user(phone)
        with _provider_refuses():
            api.post(reverse("send-code"), {"phone": phone}, format="json")

        assert not_delivered() == ["otp.not_delivered reason=send_failed"]


@pytest.mark.django_db
@pytest.mark.usefixtures("sending_on")
class TestAFailedRegistrationLeavesNothingBehind:
    def test_no_account_is_created_and_the_retry_registers(self, api):
        """Иначе повтор ответил бы «номер уже зарегистрирован» тому, кто кода не получил."""
        phone = "+79990212652"
        with _provider_refuses():
            refused = api.post(reverse("register"), {"phone": phone}, format="json")
        assert refused.status_code == 503
        # сначала присутствие: запрос дошёл до создания и был отвергнут именно так
        assert refused.json()["error"]["code"] == "OTP_NOT_SENT"
        assert User.objects.filter(phone=phone).count() == 0

        with _provider_accepts():
            retried = api.post(reverse("register"), {"phone": phone}, format="json")
        assert retried.status_code == 201
        assert User.objects.filter(phone=phone).count() == 1

    def test_the_retry_through_the_unified_endpoint_is_still_a_new_person(self, api):
        phone = "+79990222652"
        with _provider_refuses():
            api.post(reverse("send-otp"), {"phone": phone}, format="json")
        with _provider_accepts():
            retried = api.post(reverse("send-otp"), {"phone": phone}, format="json")

        assert retried.status_code == 200
        assert retried.json()["data"]["is_new_user"] is True


@pytest.mark.django_db
@pytest.mark.usefixtures("development_mode")
class TestDevelopmentModeStillLogsIn:
    """``dev_code`` — не отказ: все четыре поверхности отвечают успехом, вход работает."""

    def test_register(self, api):
        resp = api.post(reverse("register"), {"phone": "+79990312652"}, format="json")
        assert resp.status_code == 201

    def test_login(self, api):
        phone = "+79990322652"
        _existing_user(phone)
        assert api.post(reverse("login"), {"phone": phone}, format="json").status_code == 200

    def test_resend_code(self, api):
        phone = "+79990332652"
        _existing_user(phone)
        assert api.post(reverse("send-code"), {"phone": phone}, format="json").status_code == 200

    def test_the_known_code_opens_the_door(self, api, settings):
        """Сквозь: единая авторизация → код разработки → токены."""
        phone = "+79990342652"
        sent = api.post(reverse("send-otp"), {"phone": phone}, format="json")
        assert sent.status_code == 200

        assert OTPService().consume_otp(phone, settings.OTP_DEBUG_CODE) is True
