"""OTP_FIXED_DEV_CODE_ENABLED — the constant login code is an explicit opt-in.

``OTPService.send_otp`` issued ``OTP_DEBUG_CODE`` ("0000") whenever
``DEBUG`` was on and SMS sending was off. DEBUG says nothing about who can
reach the server: a DEBUG container with SMS_ENABLED unset let anyone log
in as any phone number with "0000". The fixed code now needs DEBUG AND an
explicit ``OTP_FIXED_DEV_CODE_ENABLED=true`` (sending still off); prod.py
refuses to boot with the flag on (djangoProject/tests/
test_prod_security_hardening.py).

``users/tests/conftest.py`` switches the flag ON for this package (the
pre-existing tests were written against the fixed code); the tests here
flip it explicitly in both directions.
"""
from __future__ import annotations

import importlib
import sys

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import OTPCode
from users.services import InvalidOTPError, OtpSendOutcome, OTPService


@pytest.mark.django_db
class TestTheFlagGatesTheFixedCode:
    PHONE = "+79990003001"

    def test_debug_alone_no_longer_issues_the_fixed_code(self, settings):
        settings.DEBUG = True
        settings.SMS_ENABLED = False
        settings.OTP_FIXED_DEV_CODE_ENABLED = False

        outcome = OTPService().send_otp(self.PHONE)

        # A real random code was generated, nobody received it: honest
        # "not sent" (DRF-2652), nothing kept, and "0000" opens no door.
        assert outcome is OtpSendOutcome.NOT_SENT
        assert not OTPCode.objects.filter(phone=self.PHONE).exists()
        with pytest.raises(InvalidOTPError):
            OTPService().consume_otp(self.PHONE, settings.OTP_DEBUG_CODE)

    def test_the_flag_without_debug_does_not_issue_the_fixed_code(
        self, settings, sms_delivered,
    ):
        settings.DEBUG = False
        settings.OTP_FIXED_DEV_CODE_ENABLED = True

        OTPService().send_otp(self.PHONE)

        issued = OTPCode.objects.get(phone=self.PHONE)
        # Random codes never start with "0" (DRF-2559), so this is exact.
        assert issued.code != settings.OTP_DEBUG_CODE

    def test_the_flag_with_debug_and_sending_off_issues_the_fixed_code(self, settings):
        settings.DEBUG = True
        settings.SMS_ENABLED = False
        settings.OTP_FIXED_DEV_CODE_ENABLED = True

        assert OTPService().send_otp(self.PHONE) is OtpSendOutcome.DEV_CODE
        assert OTPCode.objects.get(phone=self.PHONE).code == settings.OTP_DEBUG_CODE

    def test_sending_on_wins_over_the_flag(self, settings, sms_delivered):
        """SMS enabled -> a real code goes out even in development."""
        settings.DEBUG = True
        settings.OTP_FIXED_DEV_CODE_ENABLED = True

        assert OTPService().send_otp(self.PHONE) is OtpSendOutcome.SENT
        assert OTPCode.objects.get(phone=self.PHONE).code != settings.OTP_DEBUG_CODE

    def test_the_login_surface_refuses_instead_of_pretending(self, settings):
        """Flag off, sending off, DEBUG on: the endpoint says the code was
        not sent (503 OTP_NOT_SENT) rather than silently issuing "0000"."""
        settings.DEBUG = True
        settings.SMS_ENABLED = False
        settings.OTP_FIXED_DEV_CODE_ENABLED = False
        api = APIClient(headers={"X-App-Type": "client"})

        resp = api.post(reverse("send-otp"), {"phone": "+79990003002"}, format="json")

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "OTP_NOT_SENT"


class TestTheFlagIsReadFromTheEnvironment:
    ENV_VAR = "OTP_FIXED_DEV_CODE_ENABLED"

    def _reload_base(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv(self.ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(self.ENV_VAR, value)
        monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
        sys.modules.pop("djangoProject.settings.base", None)
        return importlib.import_module("djangoProject.settings.base")

    def test_default_is_off(self, monkeypatch):
        assert self._reload_base(monkeypatch, None).OTP_FIXED_DEV_CODE_ENABLED is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE"])
    def test_true_switches_it_on(self, monkeypatch, value):
        assert self._reload_base(monkeypatch, value).OTP_FIXED_DEV_CODE_ENABLED is True

    @pytest.mark.parametrize("value", ["false", "1", "yes", ""])
    def test_anything_else_is_off(self, monkeypatch, value):
        assert self._reload_base(monkeypatch, value).OTP_FIXED_DEV_CODE_ENABLED is False
