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
