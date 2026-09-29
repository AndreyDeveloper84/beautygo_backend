"""DRF-2646 — признак демо-салона не может молча остаться «не решённым».

На пилоте 29.09 у всех 22 тенантов ``is_demo = false``, хотя пятеро заведены
сидом демо-салонов: миграция ``tenants 0009`` дала строкам, жившим до поля,
``False``, а пометку после неё не запускали. Десять пулов выдачи клиенту
спрашивали признак и не отсекали ничего.

Узлы — парами, которые обязаны различаться (узел «у тенанта есть поле
is_demo» прошёл бы при самом дефекте):

* d1 — заведение решает признак: салон, заведённый человеком (провижининг
  салона и соло), — ``False``; сид демо ставит ``True`` сам (узел
  ``services/tests/test_seed_demo_salons.py`` — вторая половина пары);
* d2 — сторож ``tenants.W001``: салон из файла сида с ``is_demo = false``
  назван по имени; тот же салон помеченным — не назван; салон человека с
  ``false`` — не назван (это решённое «не демо», а не нерешённое);
* d3 — без базы (обычный ``manage.py check`` в CI) сторож молчит.
"""

from __future__ import annotations

import uuid

import pytest
from django.core import checks

from tenants.checks import seeded_demo_salons_are_marked
from tenants.demo_seed import demo_seed_slugs
from tenants.models import Tenant
from tenants.provisioning import ensure_tenant
from tenants.solo_provisioning import provision_solo_workspace

pytestmark = pytest.mark.django_db


def _w001(databases=("default",)) -> list[checks.CheckMessage]:
    return [m for m in seeded_demo_salons_are_marked(None, databases=list(databases)) if m.id == "tenants.W001"]


class TestD1CreationDecides:
    def test_a_salon_provisioned_by_a_person_is_not_demo(self) -> None:
        tenant, created = ensure_tenant(slug="salon-2646", name="Салон 2646")
        assert created is True
        assert tenant.is_demo is False

    def test_a_solo_workspace_is_not_demo(self) -> None:
        ws = provision_solo_workspace(
            tenant_id=uuid.uuid4(),
            slug="solo-2646",
            name="Мастер 2646",
            city="Москва",
            external_user_id="bot:max:2646",
            display_name="Мастер",
        )
        assert Tenant.all_objects.get(slug="solo-2646").is_demo is False
        assert ws is not None


class TestD2GuardNamesUndecided:
    def test_the_seed_file_has_the_known_demo_salons(self) -> None:
        # Положительный контроль источника: иначе «никого не назвал» пуст.
        assert "olhovyy-dvor" in demo_seed_slugs()

    def test_a_seeded_salon_left_false_is_named(self) -> None:
        Tenant.all_objects.create(slug="olhovyy-dvor", name="Ольховый двор", is_demo=False)
        Tenant.all_objects.create(slug="salon-2646", name="Живой салон", is_demo=False)

        found = _w001()

        assert len(found) == 1
        assert "olhovyy-dvor" in found[0].msg
        # Салон человека с false — решённое «не демо», а не нерешённое.
        assert "salon-2646" not in found[0].msg

    def test_the_same_salon_marked_is_not_named(self) -> None:
        Tenant.all_objects.create(slug="olhovyy-dvor", name="Ольховый двор", is_demo=True)
        Tenant.all_objects.create(slug="salon-2646", name="Живой салон", is_demo=False)

        assert _w001() == []  # empty-assert-ok: пара к узлу выше — тот же салон помечен


class TestD3NoDatabaseNoQuery:
    def test_plain_check_without_databases_stays_silent(self) -> None:
        Tenant.all_objects.create(slug="olhovyy-dvor", name="Ольховый двор", is_demo=False)

        assert seeded_demo_salons_are_marked(None, databases=None) == []  # empty-assert-ok: CI без базы
        # Пара: с базой тот же салон назван — молчание выше не от пустоты.
        assert len(_w001()) == 1
