"""Расписание заводится рядом с мастером, а не тридцатью пятью отправками.

Чтобы завести салон из пяти мастеров, человек делал **тридцать пять**
отправок формы: семь строк расписания на каждого, отдельным экраном,
который про мастера даже не упоминает. Здесь закрепляются обе половины
починки — вложенный блок на форме мастера и действие-пресет.

Три вещи, которые эти тесты держат помимо удобства:

* **выходной — это отсутствие часов, а не часы, которые никто не смотрит.**
  Правила не было нигде: у модели нет ни ``constraints``, ни ``clean``, а
  ``WorkingHoursSerializer.validate`` заходит внутрь только при
  ``is_working_day=True`` — ветки «выходной, а времена заданы» там нет
  вовсе. Замер контура 10.09.2026: таких строк **пять**, все воскресенья
  09:00–21:00 у пяти разных специалистов;
* **пресет — видимое решение, а не умолчание.** Четверо пилотных мастеров
  числятся работающими семь дней 10:00–19:00 без обеда; этого никто не
  вводил. Действие обязано показать часы до записи;
* **существующее расписание не затирается молча.** В API замена всех семи
  дней — явное намерение вызывающего, приславшего неделю; в админке
  «выделить всё» ставится одним движением.
"""
from __future__ import annotations

from datetime import time
from uuid import uuid4

import pytest
from django.contrib import admin as django_admin
from django.urls import reverse

from appointments.admin import SpecialistWorkingHoursInline, WorkingHoursInlineForm
from appointments.models import SpecialistWorkingHours
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant() -> Tenant:
    # Свой слаг, а не «formula-tela»: он уже занят в этой базе, и
    # столкновение читалось бы как падение предмета, а не стенда.
    return Tenant.objects.create(name="Салон часов", slug=f"hours-{uuid4().hex[:8]}")


@pytest.fixture
def operator() -> User:
    return User.objects.create_superuser(
        username="hours-operator",
        password="pw",  # pragma: allowlist secret
        email="op@example.com",
        role="admin",
    )


def _master(tenant: Tenant, name: str) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"u-{name}-{uuid4().hex[:6]}",
        password="pw",  # pragma: allowlist secret
        role="specialist",
    )
    # Профиль мастера заводится сигналом при создании пользователя с ролью
    # ``specialist``: ``create`` здесь дал бы дубль по OneToOne, а не второй
    # профиль. Подхватываем существующий и дописываем салон и имя.
    profile, _created = SpecialistProfile.objects.get_or_create(user=user)
    profile.tenant = tenant
    profile.display_name = name
    profile.save(update_fields=["tenant", "display_name"])
    return profile


def _form(**data) -> WorkingHoursInlineForm:
    base = {
        "day_of_week": 0,
        "is_working_day": True,
        "start_time": "10:00",
        "end_time": "19:00",
        "break_start": "",
        "break_end": "",
    }
    base.update(data)
    return WorkingHoursInlineForm(data=base)


# ─── блок на форме мастера ───────────────────────────────────────────────────


def test_the_hours_live_on_the_master_form():
    """Тридцать пять отправок превращаются в пять: блок смонтирован."""

    model_admin = django_admin.site._registry[SpecialistProfile]
    assert SpecialistWorkingHoursInline in model_admin.inlines


def test_the_block_does_not_offer_empty_rows_to_fill_by_hand():
    """``extra`` = 0, и это не косметика.

    Пустая форма, которую предлагают заполнить, — ровно тот путь, которым
    в базу попадают выдуманные часы. Неделя ставится пресетом.
    """

    assert SpecialistWorkingHoursInline.extra == 0


# ─── выходной — обе стороны правила ──────────────────────────────────────────


def test_a_day_off_with_times_is_refused():
    """Сторона правила, которой не было НИГДЕ.

    Ни ограничения у модели, ни ветки у сериализатора: «выходной с 10 до
    19» проходит через API и сегодня. Строка читается потребителем как
    «не работает», а в базе выглядит заполненной сменой.
    """

    form = _form(is_working_day=False, start_time="10:00", end_time="19:00")

    assert not form.is_valid()
    assert "start_time" in form.errors
    assert "end_time" in form.errors


def test_a_clean_day_off_is_accepted():
    """Положительная стража: правило отвергает противоречие, а не выходной.

    Без неё предыдущий тест зеленел бы и на форме, которая не принимает
    выходные вовсе.
    """

    form = _form(
        is_working_day=False, start_time="", end_time="",
        break_start="", break_end="",
    )

    assert form.is_valid(), form.errors


def test_an_existing_contradictory_row_is_caught_too(tenant: Tenant):
    """Правило применяется и к данным, которые уже лежат в базе.

    На контуре таких строк пять (замер 10.09.2026), и они переживут
    правку только потому, что их никто не открывал. Форма, проверяющая
    лишь новые строки, оставила бы их невидимыми навсегда — сторож,
    который не срабатывает на существующем, читается как гарантия.
    """

    master = _master(tenant, "Воскресная")
    row = SpecialistWorkingHours.objects.create(
        specialist=master, day_of_week=6, is_working_day=False,
        start_time=time(9, 0), end_time=time(21, 0),
    )

    form = WorkingHoursInlineForm(
        instance=row,
        data={
            "day_of_week": 6, "is_working_day": False,
            "start_time": "09:00", "end_time": "21:00",
            "break_start": "", "break_end": "",
        },
    )

    assert not form.is_valid()


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"start_time": "", "end_time": ""}, None),
        ({"start_time": "19:00", "end_time": "10:00"}, None),
        ({"break_start": "13:00", "break_end": ""}, None),
        ({"break_start": "14:00", "break_end": "13:00"}, None),
        ({"break_start": "08:00", "break_end": "09:00"}, None),
    ],
    ids=["без времён", "конец раньше начала", "половина перерыва",
         "перерыв наизнанку", "перерыв вне смены"],
)
def test_the_working_day_rules_match_the_serializer(patch: dict, field):
    """Правила рабочего дня повторяют ``WorkingHoursSerializer`` дословно.

    Два разных набора правил на один объект — это способ получить строку,
    которую одна дверь принимает, а другая нет.
    """

    assert not _form(**patch).is_valid()


# ─── пресет ──────────────────────────────────────────────────────────────────


def _run_action(client, masters, *, confirm: bool, overwrite: bool = False):
    payload = {
        "action": "apply_default_schedule",
        django_admin.helpers.ACTION_CHECKBOX_NAME: [str(m.pk) for m in masters],
    }
    if confirm:
        payload["confirm"] = "yes"
    if overwrite:
        payload["overwrite"] = "yes"
    return client.post(
        reverse("admin:users_specialistprofile_changelist"), payload, follow=False,
    )


def test_the_preset_shows_the_hours_before_writing_anything(
    client, operator: User, tenant: Tenant,
):
    """Первое нажатие ничего не пишет — оно показывает.

    Изготовленное умолчание уже стоит на контуре: четверо пилотных
    мастеров «работают» семь дней 10:00–19:00 без обеда, и никто этого не
    вводил. Второй раз заводить механизм, пишущий часы за человека, нельзя.
    """

    client.force_login(operator)
    master = _master(tenant, "Первая")

    response = _run_action(client, [master], confirm=False)

    assert response.status_code == 200
    assert b"10:00" in response.content
    assert SpecialistWorkingHours.objects.filter(specialist=master).count() == 0


def test_the_preset_writes_seven_rows_of_which_five_are_working(
    client, operator: User, tenant: Tenant,
):
    """Семь СТРОК и пять РАБОЧИХ дней — разные числа, и считать надо второе.

    Выходные записываются строками: пустота не отличает «выходной» от
    «расписание не заводили».
    """

    client.force_login(operator)
    master = _master(tenant, "Вторая")

    _run_action(client, [master], confirm=True)

    rows = SpecialistWorkingHours.objects.filter(specialist=master)
    assert rows.count() == 7
    assert rows.filter(is_working_day=True).count() == 5
    weekend = rows.filter(day_of_week__in=(5, 6))
    assert weekend.count() == 2
    assert not weekend.filter(is_working_day=True).exists()
    assert not weekend.exclude(start_time=None).exists(), (
        "выходной, которому оставили времена, — это третий случай "
        "изготовленного умолчания"
    )


def test_an_existing_schedule_is_not_overwritten_by_default(
    client, operator: User, tenant: Tenant,
):
    """Настоящий график не затирается «выделить всё» одним движением."""

    client.force_login(operator)
    master = _master(tenant, "Третья")
    SpecialistWorkingHours.objects.create(
        specialist=master, day_of_week=0, is_working_day=True,
        start_time=time(8, 0), end_time=time(12, 0),
    )

    _run_action(client, [master], confirm=True)

    rows = SpecialistWorkingHours.objects.filter(specialist=master)
    assert rows.count() == 1
    assert rows.first().start_time == time(8, 0)


def test_the_checkbox_is_what_makes_the_preset_overwrite(
    client, operator: User, tenant: Tenant,
):
    """Положительная стража к предыдущему: пропуск снимается отметкой.

    Без неё «не перезаписывает» зеленело бы и на действии, которое не
    пишет вообще ничего.
    """

    client.force_login(operator)
    master = _master(tenant, "Четвёртая")
    SpecialistWorkingHours.objects.create(
        specialist=master, day_of_week=0, is_working_day=True,
        start_time=time(8, 0), end_time=time(12, 0),
    )

    _run_action(client, [master], confirm=True, overwrite=True)

    rows = SpecialistWorkingHours.objects.filter(specialist=master)
    assert rows.count() == 7
    assert rows.get(day_of_week=0).start_time == time(10, 0)


def test_the_preset_drops_the_slot_cache(
    client, operator: User, tenant: Tenant, monkeypatch,
):
    """Часы, которых клиент не увидит, — починка, доказывающая себе саму себя.

    Тем же вызовом, что и писатель расписания в API: два горизонта на один
    кэш разошлись бы молча.
    """

    calls: list = []
    monkeypatch.setattr(
        "users.schedule_api._invalidate_slots",
        lambda specialist_id, date_from, date_to: calls.append(specialist_id),
    )

    client.force_login(operator)
    master = _master(tenant, "Пятая")

    _run_action(client, [master], confirm=True)

    assert calls == [master.pk]
