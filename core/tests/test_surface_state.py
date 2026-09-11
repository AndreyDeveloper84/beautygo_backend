"""`surface_state`: каждое число — против фикстуры с известным составом.

Команда читает базу и печатает числа. Тест не может сказать, что на
пилоте; он может сказать, что команда считает **то, что обещает
подпись**, и что её нули отличимы от «посчитали не то». Поэтому фикстура
здесь собрана вручную с известным составом, и каждая строка вывода
сверяется с ним дословно — вместе с источником (таблица.поле).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.core.management import call_command
from django.utils import timezone

from ai.tests.factories import make_specialist, make_user
from appointments.models import Appointment, SpecialistWorkingHours
from core.management.commands.surface_state import _FLAG_W, FILE_HEADER, FLAGS, _row
from core.measurement_subject import PULSE_ANCHORS, Anchor, gather_pulse
from goals.models import ClientGoal
from nutrition.models import FoodLog, NutritionProfile
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import GeocodeStatus, Tenant

pytestmark = pytest.mark.django_db


def _run(**kwargs) -> str:
    out = StringIO()
    call_command("surface_state", stdout=out, **kwargs)
    return out.getvalue()


@pytest.fixture
def surface():
    """База с известным составом. Числа ниже — не «какие-то», а ровно эти.

    Внимание к тому, чего в фикстуре не видно. Тенанты существуют ДО
    фикстуры: миграция `tenants.0003` сеет два, а корневой conftest при
    первом `make_specialist` подставляет третий (`test-default-tenant`).
    Все они активны, без адреса и города. Их число здесь не угадывается,
    а снимается до фикстуры и прибавляется к тому, что фикстура создала
    сама: тогда новая сидовая миграция сдвинет ожидание, а не сломает
    тест непонятным «5 != 3».
    """
    now = timezone.now()
    seeded = Tenant.all_objects.count()
    assert Tenant.all_objects.filter(is_active=False).count() == 0
    assert Tenant.all_objects.exclude(address="").count() == 0
    assert Tenant.all_objects.exclude(city="").count() == 0

    # -- специалисты: 3; с адресом 2 (у одного адрес пустой); с координатами 1
    s_addr_coords = make_specialist(display_name="С адресом и координатами")
    s_addr_coords.location_lat = D("53.2007")
    s_addr_coords.location_lng = D("45.0046")
    s_addr_coords.save()
    s_addr = make_specialist(display_name="Только адрес")
    make_specialist(display_name="Без адреса", address="")

    # -- тенанты: default (из conftest) + 2 явных = 3; активных 2;
    #    с адресом 2; с городом 1
    Tenant.objects.create(
        slug="t-full", name="Адрес и город", address="Пенза, Московская 1", city="Пенза",
    )
    Tenant.objects.create(
        slug="t-inactive", name="Только адрес, выключен",
        address="Пенза, Кирова 2", is_active=False,
    )
    assert Tenant.all_objects.count() == seeded + 3, "conftest обязан был подставить default-тенант"
    tenants_expected = {"total": seeded + 3, "active": seeded + 2, "address": 2, "city": 1}

    # -- рабочие часы: 3 строки, 1 с перерывом, 2 мастера, рабочих дней 2
    SpecialistWorkingHours.objects.create(
        specialist=s_addr_coords, day_of_week=0,
        start_time="09:00", end_time="18:00", break_start="13:00", break_end="14:00",
    )
    SpecialistWorkingHours.objects.create(
        specialist=s_addr_coords, day_of_week=1, start_time="09:00", end_time="18:00",
    )
    SpecialistWorkingHours.objects.create(
        specialist=s_addr, day_of_week=6, is_working_day=False,
    )

    # -- услуги: по одной SalonService на каждый статус связи (их четыре:
    #    unmapped, review_required, verified, not_recommendable — набор
    #    берётся из choices, а не перечисляется руками: прогон этой
    #    фикстуры нашёл четвёртый статус, которого grep по трём не видел)
    category = ServiceCategory.objects.create(name="Маникюр SS")
    template = ServiceTemplate.objects.create(
        category=category, name="Классический маникюр", name_short="Маникюр",
        duration_default=60, requires_health_check=False,
    )
    tenant = Tenant.objects.get(slug="t-full")
    services = {}
    for status in SalonService.MappingStatus:
        # Решение без происхождения не пропускают check constraints
        # (`salonservice_verified_requires_provenance` и такой же для
        # `not_recommendable`) — и это правильно: фикстура обязана быть
        # такой же честной, как данные.
        provenance = {}
        if status in (
            SalonService.MappingStatus.VERIFIED,
            SalonService.MappingStatus.NOT_RECOMMENDABLE,
        ):
            provenance = dict(
                mapping_confirmed_rule="fixture_rule_v1",
                mapping_rule_version="v1",
                mapping_confirmed_at=now,
                mapping_source_ref="фикстура surface_state",
            )
        services[status] = SalonService.objects.create(
            tenant=tenant, template=template, category=category,
            name=f"Услуга {status}", mapping_status=status, **provenance,
        )

    # -- записи: 2; за 30 дней — 1 (вторая состарена через update,
    #    потому что auto_now_add выбрасывает значение из create)
    client = make_user(role="client", first_name="Клиентка")
    base = dict(
        client=client, specialist=s_addr_coords,
        salon_service=services[SalonService.MappingStatus.VERIFIED],
        status=Appointment.Status.CONFIRMED, price=D("100.00"),
    )
    Appointment.objects.create(
        start_datetime=now + timedelta(hours=3), end_datetime=now + timedelta(hours=4), **base,
    )
    old = Appointment.objects.create(
        start_datetime=now + timedelta(days=1), end_datetime=now + timedelta(days=1, hours=1), **base,
    )
    Appointment.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=40))

    # -- питание: 2 профиля (ayla_calculated, none); 3 записи еды у 2 людей
    eater_a = make_user(role="client", first_name="Ест А")
    eater_b = make_user(role="client", first_name="Ест Б")
    NutritionProfile.objects.create(
        user=eater_a, gender="female", age=30, height_cm=165, weight_kg=60.0,
        targets_source=NutritionProfile.TargetsSource.AYLA_CALCULATED,
    )
    NutritionProfile.objects.create(user=eater_b)
    for who, dish in ((eater_a, "Борщ"), (eater_a, "Каша"), (eater_b, "Салат")):
        FoodLog.objects.create(
            user=who, dish_name=dish, calories=100, protein_g=1, fat_g=1, carbs_g=1,
            meal_type="lunch", logged_at=now,
        )

    # -- цели: активных 2, закрытых 1, людей с целью 2 (закрытая — у того
    #    же человека, что и одна из активных)
    ClientGoal.objects.create(client=eater_a, goal_key="relax", source_channel="bot")
    # DRF-1660: закрытая — состоянием, не флагом. SUPERSEDED — «закрыта
    # выбором новой», ровно то, что комментарий выше и описывает.
    ClientGoal.objects.create(
        client=eater_a, goal_key="old", source_channel="bot",
        state=ClientGoal.State.SUPERSEDED,
    )
    ClientGoal.objects.create(client=client, goal_key="beauty", source_channel="miniapp")

    return {"now": now, "tenants": tenants_expected}


# --------------------------------------------------------------------------- #
# Положительная стража: на непустой базе команда печатает не нули
# --------------------------------------------------------------------------- #

def test_tenants_are_counted_from_all_rows_not_the_active_manager(surface):
    report = _run()
    tenants = report.split("тенанты", 1)[1].split("специалисты", 1)[0]
    e = surface["tenants"]
    assert e["total"] > e["active"] > e["address"] > e["city"] > 0  # фикстура различима
    assert _row("всего", e["total"], "tenants.Tenant (все строки, включая is_active=false)") in tenants
    assert _row("активных", e["active"], "tenants.Tenant.is_active = true") in tenants
    assert _row("с адресом", e["address"], "tenants.Tenant.address <> ''") in tenants
    assert _row("с городом", e["city"], "tenants.Tenant.city <> ''") in tenants


def test_tenant_coordinates_are_counted_by_is_geocoded_not_by_null(surface):
    """§139: `pending` с координатами в расчёт НЕ входит — и в счёт тоже.

    Запрос «lat IS NOT NULL» насчитал бы двоих; единственное правило
    `Tenant.is_geocoded` — одного. Команда обязана считать правилом.
    """
    from decimal import Decimal as D

    Tenant.all_objects.create(
        slug="t-geo-ok", name="Геокодирован", latitude=D("53.2"), longitude=D("45.0"),
        geocode_status=GeocodeStatus.OK,
    )
    Tenant.all_objects.create(
        slug="t-geo-pending", name="Ждёт повтора", latitude=D("53.2"), longitude=D("45.0"),
        geocode_status=GeocodeStatus.PENDING,
    )
    assert Tenant.all_objects.filter(latitude__isnull=False).count() == 2

    report = _run()
    tenants = report.split("тенанты", 1)[1].split("специалисты", 1)[0]
    assert _row(
        "с координатами", 1,
        "tenants.Tenant.is_geocoded (geocode_status ∈ ok/confirmed И latitude, longitude И не 0/0)",
    ) in tenants
    assert _row("  ok", 1, "tenants.Tenant.geocode_status = ok") in tenants
    assert _row("  pending", 1, "tenants.Tenant.geocode_status = pending") in tenants
    # Все статусы напечатаны, включая нулевые.
    for value, _label in GeocodeStatus.choices:
        assert f"  {value:<22}:" in tenants, value


def test_specialists_address_and_coordinates(surface):
    report = _run()
    block = report.split("специалисты", 1)[1].split("рабочие часы", 1)[0]
    assert "всего                   :        3   users.SpecialistProfile (все статусы)" in block
    assert "с адресом               :        2   users.SpecialistProfile.address <> ''" in block
    assert (
        "с координатами          :        1   users.SpecialistProfile.location_lat, location_lng IS NOT NULL"
        in block
    )


def test_working_hours_rows_breaks_and_distinct_masters(surface):
    report = _run()
    block = report.split("рабочие часы", 1)[1].split("услуги", 1)[0]
    assert "всего                   :        3   appointments.SpecialistWorkingHours" in block
    assert "рабочих дней            :        2   appointments.SpecialistWorkingHours.is_working_day = true" in block
    assert (
        "с перерывом             :        1   appointments.SpecialistWorkingHours.break_start, break_end IS NOT NULL"
        in block
    )
    assert "мастеров                :        2   appointments.SpecialistWorkingHours.specialist_id (distinct)" in block


def test_salon_services_by_mapping_status(surface):
    report = _run()
    block = report.split("\nуслуги", 1)[1].split("\nзаписи", 1)[0]
    assert "SalonService всего      :        4   services.SalonService" in block
    assert "  unmapped              :        1   services.SalonService.mapping_status = unmapped" in block
    assert "  review_required       :        1   services.SalonService.mapping_status = review_required" in block
    assert "  verified              :        1   services.SalonService.mapping_status = verified" in block
    assert "  not_recommendable     :        1   services.SalonService.mapping_status = not_recommendable" in block


def test_a_status_value_outside_choices_is_printed_not_hidden(surface):
    """Данные переживают код: значение, которого в choices нет, всё равно
    в таблице. Печать «только известного» спрятала бы его в разницу между
    суммой строк и «всего»."""
    SalonService.objects.filter(mapping_status="unmapped").update(mapping_status="legacy_x")
    report = _run()
    block = report.split("\nуслуги", 1)[1].split("\nзаписи", 1)[0]
    assert "  unmapped              :        0   " in block
    assert "  'legacy_x' (вне choices):        1   " in block


def test_appointments_total_and_recent_window(surface):
    report = _run()
    block = report.split("\nзаписи", 1)[1].split("\nпитание", 1)[0]
    assert "всего                   :        2   appointments.Appointment (все статусы)" in block
    assert "за 30 дней              :        1   appointments.Appointment.created_at >= " in block

    wider = _run(recent_days=60)
    block = wider.split("\nзаписи", 1)[1].split("\nпитание", 1)[0]
    assert "за 60 дней              :        2   appointments.Appointment.created_at >= " in block


def test_nutrition_profiles_targets_source_and_eaters(surface):
    report = _run()
    block = report.split("\nпитание", 1)[1].split("\nцели", 1)[0]
    assert "профилей                :        2   nutrition.NutritionProfile" in block
    assert "  none                  :        1   nutrition.NutritionProfile.targets_source = none" in block
    assert "  unknown_legacy        :        0   " in block
    assert "  ayla_calculated       :        1   " in block
    assert "  user_entered          :        0   " in block
    assert "записей еды             :        3   nutrition.FoodLog" in block
    assert "людей с записями еды    :        2   nutrition.FoodLog.user_id (distinct)" in block


def test_goals_active_closed_and_distinct_people(surface):
    report = _run()
    block = report.split("\nцели", 1)[1]
    assert "активных                :        2   goals.ClientGoal.state = active" in block
    assert "закрытых                :        1   goals.ClientGoal.state != active" in block
    # Распределение закрытых по состояниям — рядом со счётчиком.
    assert "  superseded            :        1   goals.ClientGoal.state = superseded" in block
    assert (
        "  legacy_inactive_reason_unknown:        0   "
        "goals.ClientGoal.state = legacy_inactive_reason_unknown"
    ) in block
    assert "людей с целью           :        2   goals.ClientGoal.client_id (distinct" in block


# --------------------------------------------------------------------------- #
# Рубильники (§138)
# --------------------------------------------------------------------------- #

def test_every_listed_flag_exists_in_settings():
    """Список флагов команды — не пересказ: каждый обязан быть в settings."""
    from django.conf import settings as live

    missing = [name for name in FLAGS if not hasattr(live, name)]
    assert missing == [], missing


def test_flags_print_live_value_and_env_origin(settings, monkeypatch):
    settings.GOAL_RESOLUTION_ENABLED = False
    monkeypatch.delenv("GOAL_RESOLUTION_ENABLED", raising=False)
    settings.CROSS_DOMAIN_ENABLED = True
    monkeypatch.setenv("CROSS_DOMAIN_ENABLED", "1")

    report = _run()
    block = report.split("== РУБИЛЬНИКИ", 1)[1].split("== СОСТОЯНИЕ ПОВЕРХНОСТИ ==", 1)[0]
    assert _row(
        "GOAL_RESOLUTION_ENABLED", "ЗАПЕРТО",
        "settings.GOAL_RESOLUTION_ENABLED = False; env не задан → умолчание кода",
        label_w=_FLAG_W,
    ) in block
    assert _row(
        "CROSS_DOMAIN_ENABLED", "открыт",
        "settings.CROSS_DOMAIN_ENABLED = True; env CROSS_DOMAIN_ENABLED='1'",
        label_w=_FLAG_W,
    ) in block


# --------------------------------------------------------------------------- #
# Шапка предмета, предел, порядок
# --------------------------------------------------------------------------- #

def test_subject_and_limit_are_printed_before_any_number(surface):
    report = _run()
    subject = report.index("== ПРЕДМЕТ: кто отвечает на этот замер ==")
    limit = report.index("== ПРЕДЕЛ: что эта команда НЕ показывает ==")
    flags = report.index("== РУБИЛЬНИКИ")
    numbers = report.index("== СОСТОЯНИЕ ПОВЕРХНОСТИ ==")
    assert subject < limit < flags < numbers
    assert "СТАРТ ПРОЦЕССА БД" in report
    assert "время снятия" in report
    # Чего изнутри не видно — названо, а не пропущено молча.
    assert "изнутри НЕ видно имени контейнера и метки compose" in report
    assert "docker inspect" in report
    # Предел назван словами владельца, а не общей фразой.
    assert "ДАННЫЕ, а не ПОВЕДЕНИЕ" in report
    assert "break_start IS NULL" in report


def test_every_number_carries_its_table_and_field(surface):
    """Подпись читатель проверить не может, `таблица.поле` — может."""
    report = _run()
    body = report.split("== СОСТОЯНИЕ ПОВЕРХНОСТИ ==", 1)[1]
    rows = [ln for ln in body.splitlines() if ln.startswith("  ") and ":" in ln and "число" not in ln]
    assert len(rows) >= 25, len(rows)
    for row in rows:
        source = row.split(":", 1)[1].split(None, 1)[1]
        assert "." in source and source.split(".")[0][0].islower(), row


def test_pulse_is_live_on_a_populated_base(surface):
    report = _run()
    assert "ПУЛЬС (новейшая запись)" in report
    assert "НИ ОДНА опора не ответила" not in report
    # Возраст печатается у КАЖДОЙ опоры, не только у самой новой.
    for anchor in PULSE_ANCHORS:
        assert f"    {anchor.label:<22}:" in report, anchor.label


# --------------------------------------------------------------------------- #
# Пульс: молчание всех — «не подтверждено»; опечатка — исключение
# --------------------------------------------------------------------------- #

def test_silence_of_all_anchors_is_not_confirmed_not_green():
    """Пустая база: ни одна опора не писала. Это не свежесть — это «не
    подтверждено», и строка обязана быть в выводе, а не в отсутствии."""
    report = _run()
    assert "НИ ОДНА опора не ответила" in report
    assert "свежесть НЕ подтверждена" in report
    # Числа при этом напечатаны — нулями с источником, не пропущены.
    assert "всего                   :        0   users.SpecialistProfile" in report


def test_anchor_set_resolves_to_real_models_and_fields():
    """Целостность набора опор — отдельный тест, а не побочный эффект."""
    pulses = gather_pulse()
    assert [p.label for p in pulses] == [a.label for a in PULSE_ANCHORS]
    assert all(p.error is None for p in pulses), [p.error for p in pulses]


def test_a_typo_in_an_anchor_raises_instead_of_going_silent():
    """`ai.Mesage` обязана падать, а не делать пульс «тихим»: тихий набор
    прочитался бы как приговор машине вместо приговора набору."""
    with pytest.raises(LookupError):
        gather_pulse(anchors=(Anchor("опечатка модели", "ai.Mesage", "created_at"),))
    with pytest.raises(FieldDoesNotExist):
        gather_pulse(anchors=(Anchor("опечатка поля", "ai.Message", "creatd_at"),))


# --------------------------------------------------------------------------- #
# --write
# --------------------------------------------------------------------------- #

def test_write_replaces_the_file_with_the_report_and_a_warning_header(surface, tmp_path):
    target = tmp_path / "docs" / "SURFACE_STATE.md"
    target.parent.mkdir()
    target.write_text("правленный руками текст\n", encoding="utf-8")

    report = _run(write=str(target))
    written = target.read_text(encoding="utf-8")

    assert written.startswith(FILE_HEADER)
    assert "правка руками превращает замер в мнение" in written
    assert "правленный руками текст" not in written
    assert "== ПРЕДМЕТ: кто отвечает на этот замер ==" in written
    assert "с координатами          :        1   users.SpecialistProfile.location_lat" in written
    assert f"записано: {target}" in report


# --------------------------------------------------------------------------- #
# Путь записи — вне репозитория
# --------------------------------------------------------------------------- #

def test_the_default_write_path_is_ignored_by_git_and_not_tracked():
    """Замер, перезаписанный в отслеживаемый файл, роняет следующую выкладку:
    `git checkout` откажет «local changes would be overwritten» ещё до
    миграций. Путь по умолчанию обязан быть игнорируемым, и файла с таким
    путём в индексе быть не должно."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    path = "docs/generated/SURFACE_STATE.md"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=root, check=False, capture_output=True,
    )
    assert ignored.returncode == 0, f"{path} не игнорируется git — шаг выкладки уронит checkout"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "docs/SURFACE_STATE.md", path],
        cwd=root, check=False, capture_output=True,
    )
    assert tracked.returncode != 0, "файл состояния отслеживается — это замер, а не исходник"
