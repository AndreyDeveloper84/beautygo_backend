"""DRF-1935 — обратное направление D3: «о человеке ⇒ стирается».

DRF-1918 стережёт «стирается ⇒ выгружается». Здесь — обратное: поле профиля
мастера либо стирается, либо хранится с основанием; своё место мастера и его
соло-workspace обезличиваются, место и тенант салона — нет.

Решения главного окна 15.09 (комментарий в DRF-1935):

* ``experience_years`` → 0; ``provisioned_external_user_id`` → NULL;
  ``yookassa_account_id`` → «» (D8: реквизит выплаты = способ оплаты);
* ``timezone`` хранится (регион; записи D7 — в местном времени мастера);
* своё место (tenant NULL / соло) → обезличить + INACTIVE, строку оставить;
  место, на котором работает другой живой мастер, — не трогать и назвать
  в шагах; место салона — не трогать;
* соло-workspace → имя «Студия», адрес/город/координаты пусто, строку оставить.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.deletion_executor import BotConfirmation, execute
from users.deletion_requests import ensure_deletion_request
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


class _BotOk:
    def confirm(self, **kw):
        return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})


def _master(username: str, *, tenant: Tenant | None = None) -> User:
    u = User.objects.create_user(
        username=username, password="pass",  # pragma: allowlist secret
        role="specialist",
    )
    sp = u.specialist_profile
    sp.display_name = "Вера Мастерова"
    sp.experience_years = 7
    sp.timezone = "Europe/Samara"
    sp.provisioned_external_user_id = f"bot:max:{username}"
    sp.yookassa_account_id = f"yk-{username}"
    sp.yclients_staff_id = "yc-staff-1935"
    sp.tenant = tenant
    sp.save()
    return u


def _place(tenant: Tenant | None, address: str) -> ServiceLocation:
    return ServiceLocation.objects.create(
        tenant=tenant, label="Кабинет Веры", address=address, city="Пенза",
        geocode_source_address=address, geocode_normalized_address=f"г Пенза, {address}",
        latitude=Decimal("53.195000"), longitude=Decimal("45.018000"),
    )


def _works_at(user: User, place: ServiceLocation) -> None:
    SpecialistProfile.objects.filter(user=user).update(works_at=place)


def _delete(user: User):
    req = ensure_deletion_request(user, initiator="bot").request
    out = execute(req, bot_client=_BotOk())
    assert out.completed, out
    req.refresh_from_db()
    return req


def _tenant(slug: str, name: str, kind: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug, name=name, kind=kind, address="ул. Садовая, 5", city="Пенза",
        latitude=Decimal("53.100000"), longitude=Decimal("45.100000"),
    )


# ---------------------------------------------------------------------------
# Классификация: каждое поле профиля — стирается или хранится с основанием
# ---------------------------------------------------------------------------


class TestReverseClassification:
    def test_every_specialist_profile_field_is_erased_or_retained_with_a_reason(self):
        from users.deletion_executor import (
            SPECIALIST_PROFILE_ERASED_FIELDS,
            SPECIALIST_PROFILE_RETAINED_FIELDS,
        )

        fields = {f.name for f in SpecialistProfile._meta.concrete_fields}
        # Нижняя граница: пустая перепись прошла бы «для всех» вхолостую.
        assert len(fields) >= 20 and "display_name" in fields
        erased, retained = set(SPECIALIST_PROFILE_ERASED_FIELDS), set(SPECIALIST_PROFILE_RETAINED_FIELDS)
        assert erased & retained == set()
        assert fields - erased - retained == set()
        assert all(reason.strip() for reason in SPECIALIST_PROFILE_RETAINED_FIELDS.values())

    def test_the_owner_decided_split(self):
        from users.deletion_executor import (
            SPECIALIST_PROFILE_ERASED_FIELDS,
            SPECIALIST_PROFILE_RETAINED_FIELDS,
        )

        for field in ("experience_years", "provisioned_external_user_id", "yookassa_account_id"):
            assert field in SPECIALIST_PROFILE_ERASED_FIELDS
        assert "timezone" in SPECIALIST_PROFILE_RETAINED_FIELDS


# ---------------------------------------------------------------------------
# Значения после execute()
# ---------------------------------------------------------------------------


class TestProfileValues:
    def test_erased_fields_hold_erased_values_and_retained_ones_survive(self):
        u = _master("rev1935-values")
        _delete(u)
        sp = SpecialistProfile.objects.get(user=u)

        assert sp.experience_years == 0
        assert sp.provisioned_external_user_id is None
        assert sp.yookassa_account_id == ""
        # Хранится с основанием.
        assert sp.timezone == "Europe/Samara"
        assert sp.yclients_staff_id == "yc-staff-1935"


class TestOwnPlace:
    @staticmethod
    def _assert_anonymised(place: ServiceLocation) -> None:
        place.refresh_from_db()
        assert (place.label, place.address, place.city) == ("", "", "")
        assert (place.geocode_source_address, place.geocode_normalized_address) == ("", "")
        assert (place.latitude, place.longitude) == (None, None)
        assert place.status == LocationStatus.INACTIVE

    def test_own_place_without_a_salon_is_anonymised_and_inactive(self):
        u = _master("rev1935-own")
        place = _place(None, "ул. Первая, 1")
        _works_at(u, place)

        req = _delete(u)

        self._assert_anonymised(place)
        assert SpecialistProfile.objects.get(user=u).works_at_id == place.pk  # строка на месте
        assert req.steps["anonymised"]["tenants.ServiceLocation.own"] == 1

    def test_solo_workspace_place_is_anonymised(self):
        solo = _tenant("rev1935-solo-place", "Студия Веры", Tenant.Kind.SOLO)
        u = _master("rev1935-soloplace", tenant=solo)
        place = _place(solo, "ул. Вторая, 2")
        _works_at(u, place)

        _delete(u)

        self._assert_anonymised(place)

    def test_salon_place_is_untouched(self):
        salon = _tenant("rev1935-salon-place", "Салон Лето", Tenant.Kind.SALON)
        u = _master("rev1935-salonplace", tenant=salon)
        place = _place(salon, "ул. Третья, 3")
        _works_at(u, place)

        req = _delete(u)

        place.refresh_from_db()
        assert place.address == "ул. Третья, 3" and place.latitude is not None
        assert place.status == LocationStatus.REVIEW_REQUIRED
        assert "tenants.ServiceLocation.own" not in req.steps["anonymised"]

    def test_a_place_shared_with_a_live_master_is_untouched_and_named(self):
        leaving = _master("rev1935-shared-a")
        staying = _master("rev1935-shared-b")
        place = _place(None, "ул. Четвёртая, 4")
        _works_at(leaving, place)
        _works_at(staying, place)

        req = _delete(leaving)

        place.refresh_from_db()
        assert place.address == "ул. Четвёртая, 4" and place.latitude is not None
        assert req.steps["kept"]["tenants.ServiceLocation.shared"] == 1


class TestSoloTenant:
    def test_solo_workspace_is_anonymised_and_the_row_is_kept(self):
        solo = _tenant("rev1935-solo", "Студия Веры", Tenant.Kind.SOLO)
        u = _master("rev1935-solotenant", tenant=solo)

        req = _delete(u)

        t = Tenant.all_objects.get(pk=solo.pk)
        assert t.name == "Студия"
        assert (t.address, t.city) == ("", "")
        assert (t.geocode_source_address, t.geocode_normalized_address) == ("", "")
        assert (t.latitude, t.longitude) == (None, None)
        assert t.slug == "rev1935-solo" and t.kind == Tenant.Kind.SOLO
        assert req.steps["anonymised"]["tenants.Tenant.solo"] == 1

    def test_salon_tenant_is_untouched(self):
        salon = _tenant("rev1935-salon", "Салон Лето", Tenant.Kind.SALON)
        u = _master("rev1935-salontenant", tenant=salon)

        _delete(u)

        t = Tenant.all_objects.get(pk=salon.pk)
        assert t.name == "Салон Лето" and t.address == "ул. Садовая, 5" and t.latitude is not None
