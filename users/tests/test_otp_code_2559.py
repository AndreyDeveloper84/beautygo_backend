"""Код входа: длина из настройки, источник — ``secrets`` (DRF-2559).

До DRF-2559 ``OTP_CODE_LENGTH = 6`` не читал никто, а код был четырёхзначным
(``random.randint(1000, 9999)``): настройка лгала, и её смена не меняла ничего.

Длина — четыре цифры, и это решение, а не недосмотр: API Specification v2.0,
``POST /auth/verify-otp`` — «code: string // 4 цифры», переход 6 → 4 сделан
коммитом 15ca63b1 (08.04). Поэтому поправлена настройка, а не поведение.

Узлы:

* длина кода следует настройке — и при 4, и при 6; иначе настройка снова станет
  украшением;
* диапазон прежний, без ведущего нуля;
* путь выдачи кода (``DEBUG=False``) выдаёт код объявленной длины;
* источник — ``secrets``: подменённый ``secrets.randbelow`` определяет код, а
  сломанный ``random`` ему не мешает;
* объявлено четыре — как в спецификации.
"""

from __future__ import annotations

import random
import secrets

import pytest
from django.conf import settings as django_settings

from users.models import OTPCode
from users.services import OTPService, _new_otp_code

SAMPLES = 200


@pytest.mark.parametrize("length", [4, 6])
def test_the_code_length_follows_the_setting(settings, length):
    settings.OTP_CODE_LENGTH = length

    codes = [_new_otp_code() for _ in range(SAMPLES)]

    assert all(code.isdigit() for code in codes), codes[:5]
    assert {len(code) for code in codes} == {length}


@pytest.mark.parametrize("length", [4, 6])
def test_no_leading_zero_as_before(settings, length):
    settings.OTP_CODE_LENGTH = length

    codes = [_new_otp_code() for _ in range(SAMPLES)]

    assert all(code[0] != "0" for code in codes), [c for c in codes if c[0] == "0"]


def test_the_source_is_secrets_not_random(settings, monkeypatch):
    settings.OTP_CODE_LENGTH = 4

    def broken(*args, **kwargs):
        raise AssertionError("код входа не должен браться из random")

    monkeypatch.setattr(random, "randint", broken)
    monkeypatch.setattr(random, "random", broken)
    monkeypatch.setattr(secrets, "randbelow", lambda n: 0)

    assert _new_otp_code() == "1000"


@pytest.mark.django_db
def test_the_issued_code_has_the_declared_length(settings):
    settings.DEBUG = False
    settings.SMS_ENABLED = False
    settings.OTP_CODE_LENGTH = 6
    phone = "+79990002559"

    OTPService().send_otp(phone)

    issued = OTPCode.objects.filter(phone=phone).order_by("-created_at").first()
    assert issued is not None, "код не выпущен — проверять нечего"
    assert issued.code != django_settings.OTP_DEBUG_CODE, "проверяется не тот путь"
    assert len(issued.code) == 6


@pytest.mark.django_db
def test_the_issue_path_takes_its_code_from_secrets(settings, monkeypatch):
    """Не только функция, но и путь выдачи: обход ``_new_otp_code`` обратно к
    ``random`` с верной длиной прошёл бы узлы выше — этот его ловит."""
    settings.DEBUG = False
    settings.SMS_ENABLED = False
    settings.OTP_CODE_LENGTH = 4
    phone = "+79990002560"

    def broken(*args, **kwargs):
        raise AssertionError("код входа не должен браться из random")

    monkeypatch.setattr(random, "randint", broken)
    monkeypatch.setattr(secrets, "randbelow", lambda n: 0)

    OTPService().send_otp(phone)

    issued = OTPCode.objects.filter(phone=phone).order_by("-created_at").first()
    assert issued is not None, "код не выпущен — проверять нечего"
    assert issued.code == "1000"


def test_four_digits_are_declared_as_in_the_spec():
    """Спецификация v2.0: «code: string // 4 цифры». Не «чинить» обратно на шесть."""
    assert django_settings.OTP_CODE_LENGTH == 4
