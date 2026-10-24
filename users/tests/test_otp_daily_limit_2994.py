"""DRF-2994 — потолок кодов входа на номер за скользящие сутки.

До листа держал только минутный интервал (``OTP_RATE_LIMIT_SECONDS``):
~1440 кодов в сутки на один номер — поток SMS за наш счёт и ~4300 попыток
угадать четырёхзначный код. Длину кода не меняем: это контракт мобильных
приложений (DRF-2559).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from users.models import OTPCode
from users.services import OTPDailyLimitError, OtpSendOutcome, OTPService, RateLimitError

PHONE = "+79990012994"


@pytest.fixture
def development_mode(settings):
    """Код известен заранее, SMS не уходит — счёт кодов тот же."""
    settings.DEBUG = True
    settings.SMS_ENABLED = False


def _issued(count: int, *, hours_ago: float, phone: str = PHONE) -> None:
    """``count`` кодов, выданных ``hours_ago`` часов назад (старше минутного интервала)."""
    created = timezone.now() - timedelta(hours=hours_ago)
    for _ in range(count):
        otp = OTPCode.objects.create(
            phone=phone, code="1234", expires_at=created + timedelta(minutes=5),
        )
        # auto_now_add не даёт задать время при создании.
        OTPCode.objects.filter(pk=otp.pk).update(created_at=created)


@pytest.mark.django_db
class TestDailyLimit:
    def test_below_the_ceiling_a_code_is_issued(self, settings, development_mode):
        _issued(settings.OTP_MAX_SENDS_PER_DAY - 1, hours_ago=2)

        assert OTPService().send_otp(PHONE) is OtpSendOutcome.DEV_CODE

    def test_at_the_ceiling_the_number_is_refused(self, settings, development_mode):
        _issued(settings.OTP_MAX_SENDS_PER_DAY, hours_ago=2)

        with pytest.raises(OTPDailyLimitError):
            OTPService().send_otp(PHONE)
        assert OTPCode.objects.filter(phone=PHONE).count() == settings.OTP_MAX_SENDS_PER_DAY

    def test_codes_older_than_a_day_do_not_count(self, settings, development_mode):
        _issued(settings.OTP_MAX_SENDS_PER_DAY, hours_ago=25)

        assert OTPService().send_otp(PHONE) is OtpSendOutcome.DEV_CODE

    def test_the_ceiling_is_per_number(self, settings, development_mode):
        _issued(settings.OTP_MAX_SENDS_PER_DAY, hours_ago=2, phone="+79990022994")

        assert OTPService().send_otp(PHONE) is OtpSendOutcome.DEV_CODE

    def test_refusal_keeps_the_existing_rate_limited_contract(self):
        """Мобильные приложения уже обрабатывают RATE_LIMITED/429."""
        error = OTPDailyLimitError()
        assert isinstance(error, RateLimitError)
        assert (error.code, error.status_code) == ("RATE_LIMITED", 429)

    def test_http_answer_is_429_rate_limited(self, settings, development_mode):
        _issued(settings.OTP_MAX_SENDS_PER_DAY, hours_ago=2)
        api = APIClient(headers={"X-App-Type": "client"})

        resp = api.post(reverse("send-otp"), {"phone": PHONE}, format="json")

        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "RATE_LIMITED"
