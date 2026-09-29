"""DRF-2671 — e2e-фикстура пишет только в базу посева.

``bootstrap_e2e_wave1`` заводит АКТИВНЫЙ салон, пользователей, мастера и
записи, а ``--bind-external`` привязывает внешний id бота к выдуманному
клиенту. До DRF-2671 стража не было вовсе. Форма — у ``seed_golden``: имя
базы (подмена только в настройках, соединение остаётся тестовым —
проверяется страж, а не запись).

Узлы — парами. Узел «команда завела салон» один проходит и при живом
дефекте, поэтому рядом всегда отказ на чужой базе:

* g1 — тестовая база: салон заведён и активен; ``beautygo`` (имя dev/CI и
  стенда e2e 04.08) и ``specialist_marketplace`` (умолчание prod) — отказ с
  причиной ДО первой записи;
* g2 — база с «e2e» в имени принимается сама, не только через ``test_*``;
* g3 — ``--allow-any-db`` снимает страж явно;
* g4 — ``--bind-external`` на чужой базе не исполняется: страж стоит раньше.
"""
from __future__ import annotations

from io import StringIO
from uuid import UUID

import pytest
from django.core.management import CommandError, call_command

from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
ANCHOR = "2026-10-05T12:00:00+03:00"


def _bootstrap(*args) -> str:
    out = StringIO()
    call_command("bootstrap_e2e_wave1", "--anchor", ANCHOR, *args, stdout=out)
    return out.getvalue()


def _name_the_db(settings, name: str) -> None:
    settings.DATABASES = {**settings.DATABASES}
    settings.DATABASES["default"] = {**settings.DATABASES["default"], "NAME": name}


def _fixture_absent() -> bool:
    return (
        not Tenant.all_objects.filter(pk=TENANT_ID).exists()
        and not User.objects.filter(username__startswith="e2e-wave1-").exists()
    )


class TestG1SeedingBaseWorksAnyOtherIsRefused:
    def test_on_the_test_database_the_active_salon_is_created(self) -> None:
        _bootstrap()

        tenant = Tenant.all_objects.get(pk=TENANT_ID)
        assert tenant.slug == "e2e-wave1"
        assert tenant.is_active is True

    @pytest.mark.parametrize("name", ["beautygo", "specialist_marketplace"])
    def test_on_any_other_database_it_refuses_before_the_first_write(
        self, settings, name
    ) -> None:
        assert _fixture_absent()  # предусловие: отказ не спутать с «и так пусто»
        _name_the_db(settings, name)

        with pytest.raises(CommandError, match="нет «e2e»") as refused:
            _bootstrap()

        assert name in str(refused.value)
        assert _fixture_absent()


class TestG2TheMarkerItselfIsAccepted:
    def test_a_database_named_with_e2e_is_a_stand(self, settings) -> None:
        _name_the_db(settings, "beautygo_e2e")

        _bootstrap()

        assert Tenant.all_objects.filter(pk=TENANT_ID, is_active=True).exists()


class TestG3TheOverrideIsExplicit:
    def test_allow_any_db_overrides_the_guard(self, settings) -> None:
        _name_the_db(settings, "beautygo")

        _bootstrap("--allow-any-db")

        assert Tenant.all_objects.filter(pk=TENANT_ID).exists()


class TestG4BindExternalIsBehindTheSameGuard:
    def test_binding_is_not_reached_on_a_foreign_database(self, settings) -> None:
        users_before = User.objects.count()
        _name_the_db(settings, "specialist_marketplace")

        with pytest.raises(CommandError, match="нет «e2e»"):
            _bootstrap("--bind-external", "bot:max:2671")

        assert User.objects.count() == users_before
        assert not User.objects.filter(username="bot:max:2671").exists()
