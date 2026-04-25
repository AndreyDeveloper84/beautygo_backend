"""Tests for the template registry — render correctness + missing-key
behaviour."""
import pytest

from notifications import templates
from notifications.models import Notification


class TestTemplateRender:
    def test_appointment_created_client_renders_full_payload(self):
        t = templates.get_template("appointment_created_client")
        rendered = t.render({
            "service_name": "Маникюр",
            "specialist_name": "Елена",
            "client_name": "Анна",
            "date_time": "14:00 26.04",
            "appointment_id": "abc123",
        })
        assert rendered["title"] == "Запись подтверждена"
        assert "Маникюр" in rendered["body"]
        assert "Елена" in rendered["body"]
        assert rendered["deep_link"] == "beautygo-client://appointment/abc123"

    def test_specialist_template_uses_pro_deep_link_prefix(self):
        t = templates.get_template("appointment_created_specialist")
        rendered = t.render({
            "service_name": "Стрижка",
            "specialist_name": "Елена",
            "client_name": "Анна",
            "date_time": "14:00 26.04",
            "appointment_id": "abc123",
        })
        assert rendered["deep_link"].startswith("beautygo-pro://")

    def test_reminder_template_has_sms_text(self):
        t = templates.get_template("appointment_reminder_1h")
        assert t.channel == Notification.Channel.BOTH
        rendered = t.render({
            "specialist_name": "Елена",
            "service_name": "Маникюр",
            "date_time": "14:00 26.04",
            "address": "Пушкина 10",
            "appointment_id": "abc",
        })
        assert "Елена" in rendered["sms_text"]
        assert "Пушкина 10" in rendered["sms_text"]

    def test_missing_context_key_raises(self):
        t = templates.get_template("appointment_created_client")
        with pytest.raises(KeyError):
            t.render({"service_name": "Маникюр"})  # missing other fields

    def test_unknown_template_id_raises(self):
        with pytest.raises(KeyError):
            templates.get_template("nonexistent")

    def test_all_templates_have_unique_ids(self):
        ids = list(templates.TEMPLATES.keys())
        assert len(ids) == len(set(ids))
