"""Адрес, который видит клиент, — адрес места, не человека (§9 / L6, DRF-1687).

§9: «все старые адреса перестают быть авторитетными». До L6 адрес мастера
уходил клиенту четырьмя дорогами: карточка услуги, карточка/список
мастеров, поиск, DTO движка подбора — и пятой, самой громкой:
push-напоминанием за час до визита (``notifications/tasks.py``), где
неверный адрес отправляет человека не туда.

Здесь три сторожа:

* **``offer_address``** — единственный источник адреса для показа: пусто
  без места и при неподтверждённом месте; адрес ПОДТВЕРЖДЁННОГО места
  показывается и без геокода (адрес текстом есть раньше координат);
  ``SpecialistProfile.address`` не читается ни при каком сочетании;
* **push-напоминание** несёт адрес места; старый адрес профиля в тексте
  SMS не появляется;
* **структурный**: движок подбора и напоминание не читают ``.address``
  у мастера — оба зовут ``offer_address``. Сторож 1687 этого не видит
  (``address`` не координата), поэтому проверка своя.
"""
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest
from django.utils import timezone

from tenants.distance import offer_address
from tenants.models import GeocodeStatus, LocationStatus, ServiceLocation, Tenant
from users.models import SpecialistProfile, User

ROOT = Path(__file__).resolve().parents[2]

#: Модули, где адрес мастера уходит наружу и обязан идти через ``offer_address``.
ADDRESS_EMITTERS = (
    "ai/application/services/recommendation_engine.py",
    "notifications/tasks.py",
)


def test_emitters_take_the_address_from_the_place_not_the_profile():
    for rel in ADDRESS_EMITTERS:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("offer_address")
        ]
        reads = [
            ast.unparse(n) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "address"
        ]
        assert calls, f"{rel}: адрес не берётся через offer_address"
        assert not reads, f"{rel}: чтение .address мимо места: {reads}"


# ---------------------------------------------------------------------------
# Поведение
# ---------------------------------------------------------------------------

def _master(phone: str, tenant=None) -> SpecialistProfile:
    u = User.objects.create_user(username=f"a{phone[-4:]}", password="x", role="specialist", phone=phone)
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    # старый адрес профиля заполнен НАРОЧНО: его не должно быть видно
    p.address = "СТАРЫЙ адрес профиля, ул. Человека, 1"
    p.save(update_fields=["tenant", "address"])
    return p


def _place(tenant, address, *, confirmed: bool, geocoded: bool, confirmed_by=None) -> ServiceLocation:
    kwargs = dict(tenant=tenant, address=address, city="Пенза", geocode_provider="t")
    if geocoded:
        kwargs.update(geocode_status=GeocodeStatus.OK, latitude="53.195878", longitude="45.018316")
    if confirmed:
        kwargs.update(
            status=LocationStatus.CONFIRMED, confirmed_by=confirmed_by,
            confirmed_at=timezone.now(), confirmed_source_ref="§10",
        )
    return ServiceLocation.objects.create(**kwargs)


@pytest.mark.django_db
def test_offer_address_is_the_confirmed_place_or_nothing():
    tenant = Tenant.objects.create(slug="addr", name="A")
    op = User.objects.create_user(username="op-a", password="x", phone="+79990001800")
    m = _master("+79990001801", tenant)

    assert offer_address(m) == ""                                  # места нет — старый адрес не подставляется

    m.works_at = _place(tenant, "ул. Не подтверждённая, 2", confirmed=False, geocoded=True)
    assert offer_address(m) == ""                                  # REVIEW_REQUIRED — клиенту не называем

    m.works_at = _place(tenant, "ул. Подтверждённая, 3", confirmed=True, geocoded=False, confirmed_by=op)
    assert offer_address(m) == "ул. Подтверждённая, 3"             # подтверждено, ещё без геокода — адрес есть

    m.works_at = _place(tenant, "ул. Полная, 4", confirmed=True, geocoded=True, confirmed_by=op)
    assert offer_address(m) == "ул. Полная, 4"

    inactive = _place(tenant, "ул. Закрытая, 5", confirmed=True, geocoded=True, confirmed_by=op)
    ServiceLocation.objects.filter(pk=inactive.pk).update(status=LocationStatus.INACTIVE)
    inactive.refresh_from_db()
    m.works_at = inactive
    assert offer_address(m) == ""                                  # закрытое место — не адрес


@pytest.mark.django_db
def test_reminder_push_carries_the_place_address_not_the_profile_address():
    from appointments.models import Appointment
    from notifications.models import Notification
    from notifications.tasks import REMINDER_LEAD_MINUTES, REMINDER_TEMPLATE_ID, dispatch_appointment_reminders
    from services.models import Service, ServiceCategory

    tenant = Tenant.objects.create(slug="push", name="P")
    op = User.objects.create_user(username="op-p", password="x", phone="+79990001802")
    m = _master("+79990001803", tenant)
    m.display_name = "Мастер"
    m.status = SpecialistProfile.ProfileStatus.ACTIVE
    m.works_at = _place(tenant, "ул. Места, 7", confirmed=True, geocoded=False, confirmed_by=op)
    m.save(update_fields=["display_name", "status", "works_at"])
    client = User.objects.create_user(username="cl-p", password="x", role="client", phone="+79990001804")
    cat = ServiceCategory.objects.create(name="cat-l6", slug="cat-l6")
    svc = Service.objects.create(specialist=m, category=cat, name="Маникюр", price=1000, duration_minutes=60)
    start = timezone.now() + dt.timedelta(minutes=REMINDER_LEAD_MINUTES)
    Appointment.objects.create(
        client=client, specialist=m, service=svc, start_datetime=start,
        end_datetime=start + dt.timedelta(minutes=60), status=Appointment.Status.CONFIRMED, price=svc.price,
    )

    assert dispatch_appointment_reminders()["queued"] == 1
    row = Notification.objects.get(user=client, template_id=REMINDER_TEMPLATE_ID)
    assert row.data["address"] == "ул. Места, 7"
    assert "СТАРЫЙ" not in row.body and "Человека" not in row.body
