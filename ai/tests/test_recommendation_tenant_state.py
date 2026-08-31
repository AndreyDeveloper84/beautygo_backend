"""Состояние салона решает выдачу подбора — DRF-1430.

До этого тикета ``RecommendationEngine._fetch_candidates`` спрашивал
только про ``SpecialistProfile`` и таблицу салонов не соединял вовсе.
``Tenant.is_active=False`` прятал салон от того, кто спрашивает про
салоны (дефолтный ``_ActiveTenantManager``), а мастер этого салона
попадал в выдачу подбора нетронутым.

## Почему именно ``is_active``

У ``Tenant`` поле состояния РОВНО ОДНО. Замер схемы 31.08 на боевой
форме таблицы: ``tenants_tenant`` = id, slug, name, is_active,
created_at, updated_at — и всё. ``status`` и ``is_booking_enabled``
живут на ``SpecialistProfile`` и описывают МАСТЕРА, а не салон; их
движок читал и раньше. Так что выбора между тремя полями нет: салонное
состояние в этой схеме выражается единственным флагом.

## Правило контура, которому подчинён файл

Рядом с каждым отрицательным утверждением («мастера не видно») стоит
положительная стража НА ТЕХ ЖЕ ДАННЫХ («а этого — видно»). Отрицание
без стражи зелено и на пустой выдаче, то есть не доказывает ничего.
"""
from __future__ import annotations

import pytest

from ai.application.services.recommendation_engine import (
    RecommendationEngine,
    RecommendationQuery,
)
from ai.tests.factories import make_specialist
from tenants.models import Tenant
from users.models import SpecialistProfile


pytestmark = pytest.mark.django_db


def _names() -> list[str]:
    """Имена в выдаче подбора без кэша и без сужающих фильтров."""
    result = RecommendationEngine().recommend(
        RecommendationQuery(min_rating=4.0, limit=20),
        use_cache=False,
    )
    return [c.display_name for c in result.candidates]


def _master_of(tenant, display_name: str) -> SpecialistProfile:
    """Мастер, проходящий ВСЕ остальные фильтры движка.

    status=ACTIVE, is_available, is_booking_enabled, rating выше порога
    ``AI_SPECIALIST_MIN_RATING``. То есть если он не в выдаче — причина
    может быть только в салоне.
    """
    profile = make_specialist(
        display_name=display_name, rating=4.9, reviews_count=50,
    )
    profile.tenant = tenant
    profile.save(update_fields=["tenant"])
    return profile


class TestSalonStateGatesMatching:
    def test_master_of_a_deactivated_salon_is_not_offered(self):
        """Отключённый салон уводит своих мастеров из подбора."""
        live = Tenant.all_objects.create(
            slug="live-salon", name="Живой салон", is_active=True,
        )
        off = Tenant.all_objects.create(
            slug="off-salon", name="Отключённый салон", is_active=False,
        )
        _master_of(live, "Мастер живого салона")
        _master_of(off, "Мастер отключённого салона")

        names = _names()

        # Положительная стража: выдача непустая и включённый салон в
        # ней есть. Без неё assert ниже прошёл бы на пустом результате.
        assert "Мастер живого салона" in names, names
        assert "Мастер отключённого салона" not in names, names

    def test_reactivating_the_salon_brings_its_master_back(self):
        """Обратный ход на тех же данных.

        Тест-близнец к предыдущему: доказывает, что мастер пропал
        ИМЕННО из-за ``is_active``, а не потому, что запись не доехала
        до базы или не прошла какой-то другой фильтр.
        """
        salon = Tenant.all_objects.create(
            slug="toggled-salon", name="Переключаемый салон", is_active=False,
        )
        _master_of(salon, "Мастер переключаемого салона")

        assert "Мастер переключаемого салона" not in _names()

        salon.is_active = True
        salon.save(update_fields=["is_active"])

        assert "Мастер переключаемого салона" in _names()

    def test_a_salon_in_the_pilot_state_stays_visible(self):
        """Салон в состоянии боевого пилота остаётся в выдаче.

        ``formula-tela`` — живой салон пилота с реальными людьми и
        записями; его заводит миграция
        ``tenants.0003_seed_default_tenants`` со значением
        ``is_active=True``.

        Тест не ПРЕДПОЛАГАЕТ его состояние, а сначала его ИЗМЕРЯЕТ и
        только потом требует видимости. Если однажды пилот приедет с
        другим значением флага, упадёт замер — до того, как фильтр
        тихо спрячет салон и пилот встанет.
        """
        pilot = Tenant.all_objects.get(slug="formula-tela")

        # Замер, а не предположение.
        assert pilot.is_active is True, (
            "состояние пилота изменилось: фильтр подбора спрячет "
            "formula-tela вместе со всеми его мастерами"
        )
        assert Tenant.objects.filter(slug="formula-tela").exists()

        _master_of(pilot, "Мастер пилота")

        # Положительная стража к отрицаниям выше: на тех же правилах,
        # что уводят выключенный салон, пилот обязан остаться.
        assert "Мастер пилота" in _names()

    @pytest.mark.no_auto_tenant
    def test_a_master_without_any_salon_is_still_offered(self):
        """Мастер без салона не должен пропасть из-за нового джойна.

        ``SpecialistProfile.tenant`` — ``null=True`` (бэкфилл DRF-242.4
        не закрыт). Наивный ``filter(tenant__is_active=True)`` дал бы
        INNER JOIN и молча выкосил КАЖДЫЙ профиль без салона — ровно
        тот способ уронить пилот, ради которого тикет и заведён.
        Поэтому условие в движке написано через LEFT JOIN.

        DRF-1430 просит, чтобы состояние салона влияло на выдачу, а не
        чтобы наличие салона стало новым требованием к мастеру.
        """
        orphan = make_specialist(
            display_name="Мастер без салона", rating=4.9, reviews_count=50,
        )
        # ``update`` минует pre_save-обработчик из корневого conftest.
        SpecialistProfile.objects.filter(pk=orphan.pk).update(tenant=None)

        # Стража на предусловие: профиль действительно без салона,
        # иначе тест проверял бы не то, что заявлено.
        orphan.refresh_from_db()
        assert orphan.tenant_id is None

        assert "Мастер без салона" in _names()
