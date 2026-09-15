"""«Рабочие часы → Добавить» в админке берёт мастера и не падает в 500 (аудит 15.09 §3 п.5).

``SpecialistWorkingHoursAdmin`` брал ту же форму, что инлайн на карточке
мастера: ``WorkingHoursInlineForm``. В её ``Meta.fields`` поля ``specialist``
нет. Для инлайна это правильно — мастера задаёт родитель. Для отдельной
формы — нет: «Добавить» не показывала мастера, и сохранение шло в базу без
обязательного ``specialist_id``, то есть в 500. ``raw_id_fields =
('specialist',)`` поле объявлял, но в форме его не было.

Правило:
* отдельная форма берёт мастера и те же правила времени, что инлайн: одна
  форма наследует другую, двух наборов правил на одну строку нет;
* повтор дня у того же мастера — ошибка формы (200), а не 500: оба поля
  ``unique_together`` теперь в форме, и ``ModelForm`` сверяет их сам;
* инлайн на карточке мастера не меняется.

Клиент не пробрасывает исключение запроса: тест видит настоящий статус 500,
а не трассировку.
"""
from __future__ import annotations

from datetime import time

import pytest
from django.test import Client
from django.urls import reverse

from appointments.models import SpecialistWorkingHours
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

ADD = "admin:appointments_specialistworkinghours_add"
CHANGE = "admin:appointments_specialistworkinghours_change"
MONDAY = 0


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="hours-admin-salon", name="Салон часов")


@pytest.fixture
def master(salon):
    user = User.objects.create_user(
        username="hours-admin-master", password="x",  # pragma: allowlist secret
        role="specialist", phone="+79991974001",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.display_name = "Мастер часов"
    profile.save()
    return profile


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(
        username="hours-admin-owner", password="pw",  # pragma: allowlist secret
        email="hours-admin@example.com", role="admin",
    )


def _client(owner) -> Client:
    c = Client(raise_request_exception=False)
    c.force_login(owner)
    return c


def _working_monday(master) -> dict:
    return {
        "specialist": str(master.pk), "day_of_week": str(MONDAY), "is_working_day": "on",
        "start_time": "10:00", "end_time": "19:00", "break_start": "", "break_end": "",
    }


class TestTheAddForm:
    def test_the_add_form_offers_the_master(self, owner):
        page = _client(owner).get(reverse(ADD))
        assert page.status_code == 200, page.status_code
        assert "specialist" in page.context["adminform"].form.fields

    def test_adding_hours_for_a_master_saves_the_row(self, owner, master):
        r = _client(owner).post(reverse(ADD), _working_monday(master))
        assert r.status_code == 302, r.status_code
        row = SpecialistWorkingHours.objects.get(specialist=master, day_of_week=MONDAY)
        assert (row.start_time, row.end_time) == (time(10, 0), time(19, 0))

    def test_a_second_row_for_the_same_day_is_a_form_error_not_a_500(self, owner, master):
        SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=MONDAY, is_working_day=True,
            start_time=time(9, 0), end_time=time(18, 0),
        )
        r = _client(owner).post(reverse(ADD), _working_monday(master))
        assert r.status_code == 200, r.status_code
        assert r.context["adminform"].form.errors, "a duplicate day must be named on the form"
        assert SpecialistWorkingHours.objects.filter(specialist=master, day_of_week=MONDAY).count() == 1

    def test_a_day_off_with_times_is_still_refused(self, owner, master):
        # Правила времени те же, что у инлайна: отказ до записи.
        body = _working_monday(master) | {"is_working_day": ""}
        body.pop("is_working_day")
        r = _client(owner).post(reverse(ADD), body)
        assert r.status_code == 200, r.status_code
        assert "start_time" in r.context["adminform"].form.errors
        assert not SpecialistWorkingHours.objects.filter(specialist=master).exists()


class TestTheChangeFormAndTheInline:
    def test_editing_an_existing_row_still_saves(self, owner, master):
        row = SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=MONDAY, is_working_day=True,
            start_time=time(10, 0), end_time=time(19, 0),
        )
        body = _working_monday(master) | {"end_time": "18:00"}
        r = _client(owner).post(reverse(CHANGE, args=[row.pk]), body)
        assert r.status_code == 302, (r.status_code, r.context["adminform"].form.errors if r.context else None)
        row.refresh_from_db()
        assert row.end_time == time(18, 0)

    def test_the_inline_on_the_master_keeps_its_form_without_the_master_field(self):
        from appointments.admin import SpecialistWorkingHoursInline, WorkingHoursInlineForm

        assert SpecialistWorkingHoursInline.form is WorkingHoursInlineForm
        assert "specialist" not in WorkingHoursInlineForm.Meta.fields
