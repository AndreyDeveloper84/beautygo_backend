"""Слоты и создание записи читают ОДИН горизонт бронирования (DRF-2081).

Находка DRF-2014: внутренний слот-эндпоинт (``/api/v1/internal/specialists/
{id}/slots/``) горизонта ``BOOKING_MAX_AHEAD_DAYS`` не применял — горизонт
жил только в политике создания записи. Дата за горизонтом отдавала 200 со
слотами, а запись на любой из них отказывала ``BookingWindowError``.

Что стережётся:

* h1 — день за горизонтом: 200, ``slots`` пуст, ``unavailable_reason``
  назван (``beyond_booking_horizon``) — пустой день без причины читался бы
  как выходной; 4xx ломал бы 14-дневный фан-аут бота;
* h2 — положительная пара: день внутри горизонта — слоты есть, причины нет;
* h3 — граничный день: слоты позже ТОГО ЖЕ мгновения ``booking_horizon_end``,
  что у политики создания, отсечены; каждый отданный слот политика
  пропускает, первый отсечённый — отказывает (согласованность по факту);
* h4 — публичный эндпоинт — то же (один слот-компьютер);
* h5 — политика создания по-прежнему отказывает за горизонтом;
* g1 — перепись по AST: строку ``BOOKING_MAX_AHEAD_DAYS`` читают ровно два
  файла — settings и ``appointments/domain/booking_window.py``;
* g2 — слот-компьютер и политика зовут одну функцию ``booking_horizon_end``.
"""

from __future__ import annotations

import ast
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from appointments.domain.exceptions import BookingWindowError
from appointments.domain.policies import DefaultBookingWindowPolicy
from appointments.models import SpecialistWorkingHours
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

TOKEN = "test-internal-token-2081"
INTERNAL_URL = "/api/v1/internal/specialists/"
PUBLIC_URL = "/api/v1/specialists/"


def _midday_zone() -> str:
    """Часовой пояс, в котором мгновение горизонта приходится на ~12:00 местного.

    Часы — молчаливый параметр узла h3: граничный день делится мгновением
    ``now + N дней`` на «до» и «после», и обе половины обязаны быть непусты.
    Вместо фиксированного пояса (в 23:30 по Москве половина «после» пуста)
    пояс выбирается по текущему часу UTC. ``Etc/GMT-5`` = UTC+5 (знак в этих
    зонах обратный) — все они IANA и проходят ``validate_iana_timezone``.
    """
    offset = 12 - datetime.now(tz=timezone.utc).hour  # в [-11, 12]
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


TZ = _midday_zone()


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN
    settings.BOOKING_MAX_AHEAD_DAYS = 60


@pytest.fixture
def specialist(db):
    tenant = Tenant.objects.create(slug="drf2081-t", name="DRF2081")
    u = User.objects.create_user(
        username="drf2081_spec", password="x", role="specialist", phone="+79996208101",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "DRF2081 Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = TZ
    p.save()
    # Каждый день рабочий и длинный: на граничном дне обязаны быть слоты и до,
    # и после мгновения горизонта — иначе h3 доказывал бы отсечение пустотой.
    for dow in range(7):
        SpecialistWorkingHours.objects.create(
            specialist=p, day_of_week=dow, is_working_day=True,
            start_time="00:30", end_time="23:30",
        )
    return p


@pytest.fixture
def service(specialist):
    category = ServiceCategory.objects.create(name="DRF2081 Cat", slug="drf2081-cat")
    return Service.objects.create(
        specialist=specialist, category=category, name="DRF2081 Svc",
        price=Decimal("1000.00"), duration_minutes=30, is_active=True, buffer_after_minutes=0,
    )


def _internal() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    return c


def _public() -> APIClient:
    """Клиентское приложение: X-App-Type client + вошедший клиент (публичный каталог — под JWT)."""
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    user, _ = User.objects.get_or_create(
        username="drf2081_client", defaults={"role": "client", "password": "x"}
    )
    c.force_authenticate(user=user)
    return c


def _get(client: APIClient, base: str, specialist, service, day) -> dict:
    r = client.get(f"{base}{specialist.id}/slots/?service_id={service.id}&date={day.isoformat()}")
    assert r.status_code == 200, r.content
    return r.json()


def _horizon_end_local() -> datetime:
    return (datetime.now(tz=timezone.utc) + timedelta(days=60)).astimezone(ZoneInfo(TZ))


class TestTheHorizonIsOneForSlotsAndBookings:
    def test_h1_a_day_beyond_the_horizon_is_empty_and_says_why(self, specialist, service):
        day = (_horizon_end_local() + timedelta(days=2)).date()

        body = _get(_internal(), INTERNAL_URL, specialist, service, day)

        assert body["date"] == day.isoformat()
        assert body["slots"] == [] and body["unavailable_reason"] == "beyond_booking_horizon"

    def test_h2_a_day_inside_the_horizon_is_served_as_before(self, specialist, service):
        day = (datetime.now(tz=ZoneInfo(TZ)) + timedelta(days=3)).date()

        body = _get(_internal(), INTERNAL_URL, specialist, service, day)

        assert body["date"] == day.isoformat() and len(body["slots"]) > 20
        assert "unavailable_reason" not in body

    def test_h3_the_boundary_day_is_cut_at_the_same_instant_the_booking_policy_uses(
        self, specialist, service
    ):
        end_local = _horizon_end_local()
        # Мгновение горизонта не должно упираться в край рабочего дня: иначе
        # одна из двух половин (до/после) пуста и узел ничего не отсекает.
        # Пояс подобран под полдень (``_midday_zone``), проверка — положительная стража.
        assert time(11, 0) <= end_local.time() < time(13, 0), (TZ, end_local)
        day = end_local.date()

        body = _get(_internal(), INTERNAL_URL, specialist, service, day)
        served = [datetime.fromisoformat(s) for s in body["slots"]]

        assert served, "на граничном дне обязаны быть слоты до мгновения горизонта"
        assert max(served) <= end_local and min(served) < end_local
        # Согласованность по факту: политика создания принимает каждый отданный
        # слот и отказывает первому отсечённому.
        policy = DefaultBookingWindowPolicy()
        for slot in served:
            policy.validate_booking_window(slot)
        first_cut = max(served) + timedelta(minutes=30)
        assert first_cut.time() <= time(23, 0)  # сетка ещё внутри рабочего дня — слот был бы, не будь горизонта
        with pytest.raises(BookingWindowError):
            policy.validate_booking_window(first_cut)

    def test_h4_the_public_endpoint_shares_the_same_cut(self, specialist, service):
        day = (_horizon_end_local() + timedelta(days=2)).date()
        body = _get(_public(), PUBLIC_URL, specialist, service, day)
        assert body["slots"] == [] and body["unavailable_reason"] == "beyond_booking_horizon"

        inside = (datetime.now(tz=ZoneInfo(TZ)) + timedelta(days=3)).date()
        assert len(_get(_public(), PUBLIC_URL, specialist, service, inside)["slots"]) > 20

    def test_h5_the_booking_policy_still_refuses_beyond_the_horizon(self):
        policy = DefaultBookingWindowPolicy()
        policy.validate_booking_window(datetime.now(tz=timezone.utc) + timedelta(days=59))
        with pytest.raises(BookingWindowError):
            policy.validate_booking_window(datetime.now(tz=timezone.utc) + timedelta(days=61))


class TestOneSource:
    ROOT = Path(__file__).resolve().parents[2]

    def _readers(self) -> set[str]:
        readers: set[str] = set()
        for path in self.ROOT.rglob("*.py"):
            rel = path.relative_to(self.ROOT).as_posix()
            if "/tests/" in rel or "/migrations/" in rel or rel.startswith((".venv", "venv")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # Три написания: строка в getattr(settings, "…"), атрибут settings.… и
                # само имя (объявление в settings или прямой импорт). Любое — читатель.
                if isinstance(node, ast.Constant) and node.value == "BOOKING_MAX_AHEAD_DAYS":
                    readers.add(rel)
                elif isinstance(node, ast.Attribute) and node.attr == "BOOKING_MAX_AHEAD_DAYS":
                    readers.add(rel)
                elif isinstance(node, ast.Name) and node.id == "BOOKING_MAX_AHEAD_DAYS":
                    readers.add(rel)
        return readers

    def test_g1_the_setting_has_exactly_two_readers(self):
        assert self._readers() == {
            "djangoProject/settings/base.py",
            "appointments/domain/booking_window.py",
        }, self._readers()

    def test_g2_slots_and_the_booking_policy_call_the_same_horizon_function(self):
        callers: dict[str, bool] = {}
        for rel in ("users/specialists_api.py", "appointments/domain/policies.py"):
            tree = ast.parse((self.ROOT / rel).read_text(encoding="utf-8"))
            callers[rel] = any(
                isinstance(n, ast.Call)
                and "booking_horizon_end" in (getattr(n.func, "id", ""), getattr(n.func, "attr", ""))
                for n in ast.walk(tree)
            )
        assert callers == {"users/specialists_api.py": True, "appointments/domain/policies.py": True}, callers
