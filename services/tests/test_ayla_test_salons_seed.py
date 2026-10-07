"""Сид двух тест-салонов владельца — ``services/seeds/ayla_test_salons.json``.

Файл читает тот же ``seed_demo_salons``, поэтому поведение команды здесь не
перепроверяется (оно в ``test_seed_demo_salons.py``). Здесь — свойства
именно этого файла, которые сломаются молча, если его поправить:

* все пары «категория → шаблон» разрешаются в настоящем каноническом
  каталоге (иначе команда на стенде падает ``Unresolved …``);
* салоны заводятся демонстрационными и запертыми, а ``--activate`` этого
  файла открывает только их — пять демо-салонов из основного сида остаются
  запертыми;
* ни один слаг не защищённый, у мастеров нет телефона, ни одна услуга не
  требует анкеты здоровья;
* у мастеров есть график, а значит слоты: без графика их ноль (так
  ведёт себя основной демо-сид, у которого блока ``working_hours`` нет);
* город салона записан — пустой город выводит салон из городского поиска.
"""
from __future__ import annotations

import json
from datetime import date, time, timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from appointments.models import SpecialistWorkingHours

from services.models import SalonService, SpecialistService
from tenants.models import Tenant
from tenants.protected_slugs import PROTECTED_SLUGS
from users.models import SpecialistProfile

SEEDS = Path(__file__).resolve().parents[1] / "seeds"
CANONICAL = SEEDS / "canonical_catalog_2026-07.json"
DEMO_SALONS = SEEDS / "demo_salons_2026-08.json"
TEST_SALONS = SEEDS / "ayla_test_salons.json"

#: Решение владельца 04.10 — слаги литералом, а не из файла: узел, который
#: читает ожидание из того же файла, согласился бы с любой его правкой.
SLUGS = ("ayla-test-esthetics", "ayla-test-massage")

#: Слоты 60-минутной услуги в рабочем дне 10:00–19:00 при шаге 30 минут:
#: последний старт 18:00, итого 17. Литералом — узел, который считает число
#: той же арифметикой, что движок, согласится с любой ошибкой движка.
SLOTS_IN_A_10_TO_19_DAY = 17

TOKEN = "test-ayla-internal-token-test-salons"


@pytest.fixture
def catalog(db):
    call_command("seed_canonical_catalog", "--file", str(CANONICAL), verbosity=0)


def _seed(*args: str, path: Path = TEST_SALONS) -> str:
    out = StringIO()
    call_command("seed_demo_salons", "--file", str(path), *args, stdout=out)
    return out.getvalue()


def test_the_file_names_exactly_the_two_owner_salons_and_no_protected_one():
    salons = json.loads(TEST_SALONS.read_text(encoding="utf-8"))["salons"]
    slugs = tuple(sorted(s["slug"] for s in salons))
    assert slugs == SLUGS
    assert not set(slugs) & PROTECTED_SLUGS


@pytest.mark.django_db
def test_seeds_locked_demo_salons_on_canonical_templates(catalog):
    _seed("--apply")
    tenants = Tenant.all_objects.filter(slug__in=SLUGS)
    assert tenants.count() == 2
    assert set(tenants.values_list("is_demo", "is_active")) == {(True, False)}

    masters = SpecialistProfile.objects.filter(tenant__slug__in=SLUGS)
    assert masters.count() == 2
    assert set(masters.values_list("status", "is_booking_enabled")) == {
        (SpecialistProfile.ProfileStatus.PENDING, False)
    }
    assert set(masters.values_list("user__phone", flat=True)) == {None}

    services = SalonService.objects.filter(tenant__slug__in=SLUGS)
    assert services.count() == 4
    assert all(s.template_id for s in services)
    assert set(services.values_list("requires_health_check", flat=True)) == {False}
    assert SpecialistService.objects.filter(tenant__slug__in=SLUGS, is_active=True).count() == 4


@pytest.mark.django_db
def test_activating_this_file_opens_only_its_two_salons(catalog):
    _seed("--apply", path=DEMO_SALONS)
    _seed("--apply")
    other_demo = Tenant.all_objects.filter(is_demo=True).exclude(slug__in=SLUGS)
    assert other_demo.count() == 5

    _seed("--activate", "--apply")

    opened = Tenant.all_objects.filter(slug__in=SLUGS)
    assert set(opened.values_list("is_active", flat=True)) == {True}
    assert set(
        SpecialistProfile.objects.filter(tenant__slug__in=SLUGS).values_list(
            "status", "is_booking_enabled",
        )
    ) == {(SpecialistProfile.ProfileStatus.ACTIVE, True)}
    assert not other_demo.filter(is_active=True).exists()
    assert not SpecialistProfile.objects.filter(
        tenant__in=other_demo, is_booking_enabled=True,
    ).exists()


# --------------------------------------------------------------------- #
# график и слоты
# --------------------------------------------------------------------- #
@pytest.fixture
def api(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    return client


def _next_working_day() -> date:
    day = date.today() + timedelta(days=1)
    while day.weekday() == 6:  # воскресенье — выходной в файле
        day += timedelta(days=1)
    return day


def _slots(api: APIClient, profile: SpecialistProfile) -> int:
    edge = SpecialistService.objects.filter(
        specialist=profile, is_active=True, duration_minutes=60,
    ).first()
    assert edge is not None, profile
    r = api.get(
        f"/api/v1/internal/specialists/{profile.pk}/slots/",
        {"service_id": str(edge.salon_service_id), "date": _next_working_day().isoformat()},
    )
    assert r.status_code == 200, r.content[:200]
    data = r.json()
    data = data.get("data", data) if isinstance(data, dict) else data
    return len(data if isinstance(data, list) else data["slots"])


@pytest.mark.django_db
def test_without_a_working_hours_block_an_active_master_has_no_slots(catalog, api):
    """Контраст к узлу ниже: основной демо-сид графика не задаёт."""
    _seed("--apply", path=DEMO_SALONS)
    _seed("--activate", "--apply", path=DEMO_SALONS)
    profile = SpecialistProfile.objects.filter(
        tenant__is_demo=True, specialist_services__duration_minutes=60,
    ).first()
    assert profile is not None
    assert profile.status == SpecialistProfile.ProfileStatus.ACTIVE
    assert not SpecialistWorkingHours.objects.filter(specialist=profile).exists()
    assert _slots(api, profile) == 0


@pytest.mark.django_db
def test_the_working_hours_block_gives_every_master_a_week_and_slots(catalog, api):
    out = _seed("--apply")
    assert "working_hours: written=2 kept_existing=0" in out
    _seed("--activate", "--apply")

    masters = SpecialistProfile.objects.filter(tenant__slug__in=SLUGS)
    assert masters.count() == 2
    for profile in masters:
        week = {
            wh.day_of_week: wh
            for wh in SpecialistWorkingHours.objects.filter(specialist=profile)
        }
        assert sorted(week) == list(range(7))
        for day in range(6):
            assert week[day].is_working_day
            assert (week[day].start_time, week[day].end_time) == (time(10), time(19))
        sunday = week[6]
        assert not sunday.is_working_day
        assert (sunday.start_time, sunday.end_time) == (None, None)
        assert _slots(api, profile) == SLOTS_IN_A_10_TO_19_DAY


@pytest.mark.django_db
def test_a_schedule_that_already_exists_is_left_as_it_is(catalog):
    _seed("--apply")
    profile = SpecialistProfile.objects.get(tenant__slug="ayla-test-massage")
    SpecialistWorkingHours.objects.filter(specialist=profile, day_of_week=0).update(
        start_time=time(12), end_time=time(16),
    )

    out = _seed("--apply")

    assert "working_hours: written=0 kept_existing=2" in out
    monday = SpecialistWorkingHours.objects.get(specialist=profile, day_of_week=0)
    assert (monday.start_time, monday.end_time) == (time(12), time(16))


@pytest.mark.django_db
def test_a_bad_working_hours_block_is_refused_before_anything_is_written(catalog, tmp_path):
    document = json.loads(TEST_SALONS.read_text(encoding="utf-8"))
    document["salons"][0]["working_hours"] = {
        "working_days": [0, 1, 2], "start": "19:00", "end": "10:00",
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CommandError, match="working_hours"):
        _seed("--apply", path=path)

    assert not Tenant.all_objects.filter(slug__in=SLUGS).exists()


@pytest.mark.django_db
def test_the_salon_city_is_written(catalog):
    _seed("--apply")
    assert set(
        Tenant.all_objects.filter(slug__in=SLUGS).values_list("city", flat=True)
    ) == {"Пенза"}
