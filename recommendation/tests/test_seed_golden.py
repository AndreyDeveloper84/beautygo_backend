"""``seed_golden --scenario p7`` — посев стенда для cross-boundary golden бота.

Проверяется не «команда отработала», а то, что обещано боту
(``ai-bot-platform/tests/cross_boundary/README.md``): после посева полка
отвечает ``ELIG_EXCLUDED_NOT_RECOMMENDABLE`` с непустой полкой «что есть»,
а после ``--verify-one`` — кандидатом. Это та же ручка, тот же путь.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from services.models import SalonService
from users.models import SpecialistProfile

URL = "/api/v1/internal/me/catalog/recommendations/"
TOKEN = "test-ayla-internal-token-golden"

pytestmark = pytest.mark.django_db


def _seed(*args) -> str:
    out = StringIO()
    call_command("seed_golden", "--scenario", "p7", *args, stdout=out)
    return out.getvalue()


def _ask(settings) -> dict:
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN
    client = APIClient()
    resp = client.post(
        URL,
        {},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        HTTP_X_EXTERNAL_USER_ID="bot:golden-p7",
    )
    assert resp.status_code == 200, resp.content[:300]
    return resp.json()["data"]


class TestTheSeedProducesThePilotShapedEmptiness:
    def test_seeded_masters_are_visible_but_not_recommendable(self, settings) -> None:
        out = _seed()

        assert SpecialistProfile.objects.filter(tenant__slug="golden-p7").count() == 3
        assert "'review_required': 3" in out

        data = _ask(settings)
        layer_2 = data["layer_2_ayla_picks"]
        assert layer_2["items"] == []
        assert "ELIG_EXCLUDED_NOT_RECOMMENDABLE" in layer_2["reason_codes"], layer_2
        # Полка «что вообще есть» живёт при нуле допущенных — категория видна.
        assert data["layer_3_explore"]["categories"], data["layer_3_explore"]
        # Слой 1 — решения не принимали: истории у нового клиента нет.
        assert data["layer_1_your_places"] == {"items": [], "reason_codes": []}

    def test_it_is_idempotent(self) -> None:
        _seed()
        second = _seed()
        assert SpecialistProfile.objects.filter(tenant__slug="golden-p7").count() == 3
        assert "мастеров создано=0" in second

    def test_verify_one_puts_a_candidate_on_the_shelf(self, settings) -> None:
        """Положительная стража golden'а бота: после этого P7 обязан покраснеть."""
        _seed()
        before = _ask(settings)["layer_2_ayla_picks"]
        assert before["items"] == [], "стража: до подтверждения полка пуста"

        out = _seed("--verify-one")
        assert "подтверждено сейчас=1" in out
        assert SalonService.objects.filter(
            tenant__slug="golden-p7", mapping_status=SalonService.MappingStatus.VERIFIED
        ).count() == 1

        after = _ask(settings)["layer_2_ayla_picks"]
        assert len(after["items"]) == 1, after
        assert "ELIG_EXCLUDED_NOT_RECOMMENDABLE" not in after["reason_codes"] or after["items"]


class TestItRefusesToSeedANonStandDatabase:
    def test_a_database_without_golden_in_its_name_is_refused(self, settings) -> None:
        # Имя базы подменяется только в настройках — соединение остаётся
        # тестовым; проверяется сторож, а не запись.
        settings.DATABASES = {**settings.DATABASES}
        settings.DATABASES["default"] = {**settings.DATABASES["default"], "NAME": "beautygo"}
        with pytest.raises(CommandError, match="golden"):
            _seed()

    def test_allow_any_db_overrides_the_guard(self, settings) -> None:
        settings.DATABASES = {**settings.DATABASES}
        settings.DATABASES["default"] = {**settings.DATABASES["default"], "NAME": "beautygo"}
        out = _seed("--allow-any-db")
        assert "мастеров создано=3" in out


class TestTheP1SeedIsBookableAndDeliberatelyDisagreesOnDuration:
    """``--scenario p1``: мастер VERIFIED, расписание на все дни, и два числа
    длительности намеренно разные — чтобы golden бота по числу видел, какой
    слой ушёл в бронь (матрица P1 PARTIAL: «подменяются молча»)."""

    def test_seed_shape(self) -> None:
        from appointments.models import SpecialistWorkingHours
        from services.models import SpecialistService

        out = StringIO()
        call_command("seed_golden", "--scenario", "p1", stdout=out)
        text = out.getvalue()

        edge = SpecialistService.objects.get(salon_service__name="Golden manicure P1")
        assert edge.salon_service.mapping_status == SalonService.MappingStatus.VERIFIED
        assert edge.salon_service.duration_minutes == 45
        assert edge.duration_minutes == 60
        assert str(edge.price) == "1500.00"
        # Медицинский вопрос ОТВЕЧЕН (False), не отсутствует: без шаблона
        # каскад даёт None, и бронь уходит в handoff HEALTH_CHECK_UNKNOWN.
        assert edge.resolved_requires_health_check() is False
        assert SpecialistWorkingHours.objects.filter(specialist=edge.specialist).count() == 7
        assert "НАМЕРЕННО разные" in text

    def test_booking_records_the_salon_duration_not_the_edge(self, settings) -> None:
        """Тот же факт, что нашёл golden бота, — здесь его держит каталог:
        бронь берёт SalonService.duration_minutes (45), показ — ребро (60).
        Изменится resolver — покраснеет здесь, а не только на стенде бота."""
        from datetime import date, timedelta

        from services.models import SpecialistService

        call_command("seed_golden", "--scenario", "p1")
        edge = SpecialistService.objects.get(salon_service__name="Golden manicure P1")
        settings.AYLA_INTERNAL_API_TOKEN = TOKEN
        client = APIClient()
        headers = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}", "HTTP_X_EXTERNAL_USER_ID": "bot:golden-p1"}
        ident = client.get("/api/v1/internal/me/identity/", **headers).json()["data"]
        day = (date.today() + timedelta(days=3)).isoformat()
        start = f"{day}T12:00:00+03:00"
        body = {
            "client_id": ident["ayla_user_id"],
            "specialist_id": str(edge.specialist_id),
            "service_id": str(edge.salon_service_id),
            "start_datetime": start,
        }
        r1 = client.post(
            "/api/v1/internal/appointments/", body, format="json",
            HTTP_X_IDEMPOTENCY_KEY="golden-p1-test", **headers,
        )
        assert r1.status_code in (200, 201), r1.content[:300]
        created = r1.json()["data"]
        assert created["snapshot_duration_minutes"] == 45, "бронь взяла не SalonService.duration"
        assert created["snapshot_price"] == "1500.00"
        r2 = client.post(
            "/api/v1/internal/appointments/", body, format="json",
            HTTP_X_IDEMPOTENCY_KEY="golden-p1-test", **headers,
        )
        assert r2.json()["data"]["id"] == created["id"], "тот же ключ — та же бронь"
