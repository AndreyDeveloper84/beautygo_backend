"""Tests for ai.redaction — RU phone + email scrubbing."""
import pytest

from ai.redaction import (
    EMAIL_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    has_pii,
    redact_pii,
)


class TestRedactPhone:
    @pytest.mark.parametrize("phone", [
        "+79991234567",
        "+7 999 123 45 67",
        "+7 (999) 123-45-67",
        "+7-999-123-45-67",
        "89991234567",
        "8 (999) 123-45-67",
        "79991234567",
    ])
    def test_redacts_ru_phone_formats(self, phone):
        text = f"Звони мне: {phone} спасибо"
        out = redact_pii(text)
        assert PHONE_PLACEHOLDER in out
        assert phone not in out

    def test_does_not_redact_short_digit_runs(self):
        # 5 digits — order number, not a phone
        text = "Заказ #12345 готов"
        assert redact_pii(text) == text

    def test_does_not_redact_long_id(self):
        # 15+ digits — definitely not a phone
        text = "ID транзакции 879990001112233 верифицирован"
        assert redact_pii(text) == text


class TestRedactEmail:
    @pytest.mark.parametrize("email", [
        "user@example.com",
        "first.last@sub.example.co.uk",
        "user+tag@gmail.com",
        "USER_NAME@DOMAIN.RU",
    ])
    def test_redacts_emails(self, email):
        text = f"Пиши на {email} please"
        out = redact_pii(text)
        assert EMAIL_PLACEHOLDER in out
        assert email not in out

    def test_does_not_redact_at_without_tld(self):
        # Just an @ mention, no email
        text = "@admin посмотри пожалуйста"
        assert redact_pii(text) == text


class TestEdgeCases:
    def test_empty_string(self):
        assert redact_pii("") == ""

    def test_no_pii_unchanged(self):
        text = "Хочу записаться на маникюр в субботу днём"
        assert redact_pii(text) == text

    def test_idempotent(self):
        text = "Звони +79991234567 или пиши user@example.com"
        once = redact_pii(text)
        twice = redact_pii(once)
        assert once == twice

    def test_multiple_pii_in_one_string(self):
        text = "Контакты: +7 999 123-45-67 и user@example.com и 89992223344"
        out = redact_pii(text)
        assert "+7" not in out
        assert "@example.com" not in out
        assert out.count(PHONE_PLACEHOLDER) == 2
        assert out.count(EMAIL_PLACEHOLDER) == 1


class TestHasPii:
    def test_detects_phone(self):
        assert has_pii("+79991234567")

    def test_detects_email(self):
        assert has_pii("user@example.com")

    def test_clean_text(self):
        assert not has_pii("Хочу маникюр в субботу")

    def test_empty(self):
        assert not has_pii("")
