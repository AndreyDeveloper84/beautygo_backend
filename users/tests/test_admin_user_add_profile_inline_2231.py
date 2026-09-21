"""«Добавить пользователя» с блоком «Профиль клиента» не падает 500 (DRF-2231).

Стенд, 13:06:54–13:11:14 MSK: владелец заводит мастера кнопкой «+» из формы
салона — ``POST /admin/users/user/add/?_to_field=id&_popup=1``, — и
«Сохранить» даёт 500 шесть раз подряд, у всех ``IntegrityError: duplicate
key … "users_profile_user_id_key"``. POST в то же время без заполненного
блока профиля проходили: инлайн не сохранялся.

Механизм: ``users/signals.py::create_user_profile`` на ``post_save``
создаёт ``Profile`` для ролей ``client`` / ``specialist``, а
``UserAdmin.inlines = [ProfileInline]`` при заполненном блоке сохраняет
ВТОРОЙ ``Profile`` того же человека — уникальность ``user_id`` отказывает.
Существующие тесты формы добавления (``test_admin_user_add_finds_existing_
2067``) шлют блок пустым (``TOTAL_FORMS=0``) и этот путь не видели.

Узлы бьют в тот же URL, что у владельца (popup — путь «+» из формы салона):
успех в popup — 200 с ответом закрытия окна (``popup_response``), а не 302.

1. мастер с заполненным профилем: успех, ровно ОДИН ``Profile``, в нём то,
   что ввёл владелец (а не пустая строка сигнала);
2. то же для клиента;
3. блок не заполнен: один ``Profile`` — от сигнала, как и раньше;
4. роль ``admin``: сигнал профиля не создаёт — блок пуст → профиля нет;
   блок заполнен → профиль создаётся из блока (прежнее поведение, закреплено);
5. ``SpecialistProfile`` у мастера по-прежнему ровно один;
6. правка существующего пользователя через тот же блок обновляет профиль,
   дубля нет (граница: правка не должна сломаться от правки добавления).
"""
from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from users.models import Profile, SpecialistProfile, User

pytestmark = pytest.mark.django_db

PASSWORD = "Sup3rSecret!42"  # pragma: allowlist secret
POPUP = "?_to_field=id&_popup=1"


def _admin_client() -> Client:
    staff = User.objects.create_superuser(
        username="drf2231-operator",
        email="operator-2231@example.com",
        password=PASSWORD,
        role="admin",
    )
    client = Client()
    client.force_login(staff)
    return client


def _add(client: Client, *, username: str, role: str, profile: dict | None):
    data = {
        "username": username,
        "password1": PASSWORD,
        "password2": PASSWORD,
        "role": role,
        "phone": "",
        "email": f"{username}@example.com",
        "_popup": "1",
        "_to_field": "id",
        "profile-TOTAL_FORMS": "1",
        "profile-INITIAL_FORMS": "0",
        "profile-MIN_NUM_FORMS": "0",
        "profile-MAX_NUM_FORMS": "1",
        "profile-0-id": "",
        "profile-0-user": "",
        "profile-0-full_name": "",
        "profile-0-city": "",
        "profile-0-bio": "",
    }
    for key, value in (profile or {}).items():
        data[f"profile-0-{key}"] = value
    return client.post(reverse("admin:users_user_add") + POPUP, data)


def _closed_popup(resp) -> bool:
    """Успех добавления в popup — страница закрытия окна с id нового объекта."""
    templates = [t.name for t in (getattr(resp, "templates", None) or [])]
    return resp.status_code == 200 and "admin/popup_response.html" in templates


def _profiles(username: str) -> list[Profile]:
    return list(Profile.objects.filter(user__username=username))


@pytest.mark.parametrize("role", ["specialist", "client"], ids=["master", "client"])
def test_filled_profile_block_saves_one_profile_with_the_owners_input(role):
    client = _admin_client()

    resp = _add(
        client,
        username=f"drf2231-{role}",
        role=role,
        profile={"full_name": "Анна Петрова", "city": "Москва"},
    )

    assert _closed_popup(resp), resp.content[:2000]
    profiles = _profiles(f"drf2231-{role}")
    assert len(profiles) == 1
    assert profiles[0].full_name == "Анна Петрова"
    assert profiles[0].city == "Москва"
    # Форма салона берёт из закрытия окна id нового человека — он там.
    new_id = str(User.objects.get(username=f"drf2231-{role}").pk)
    assert new_id in resp.context["popup_response_data"]


def test_fields_outside_the_block_are_kept_not_reset():
    """Форма ложится поверх строки сигнала, а не заменяет её умолчаниями."""
    from unittest.mock import patch

    from users import signals

    original = signals.Profile.objects.create

    def _prefilled(**kwargs):
        # Сигнал, который когда-нибудь заполнит профиль сам.
        return original(**kwargs, experience_years=7)

    client = _admin_client()
    with patch.object(signals.Profile.objects, "create", side_effect=_prefilled):
        resp = _add(
            client,
            username="drf2231-kept",
            role="specialist",
            profile={"full_name": "Анна Петрова"},
        )

    assert _closed_popup(resp), resp.content[:2000]
    profile = Profile.objects.get(user__username="drf2231-kept")
    assert profile.full_name == "Анна Петрова"
    assert profile.experience_years == 7


def test_empty_profile_block_keeps_the_signal_profile():
    client = _admin_client()

    resp = _add(client, username="drf2231-empty", role="specialist", profile=None)

    assert _closed_popup(resp), resp.content[:2000]
    assert len(_profiles("drf2231-empty")) == 1


def test_admin_role_with_an_empty_block_has_no_profile():
    client = _admin_client()

    resp = _add(client, username="drf2231-admin-empty", role="admin", profile=None)

    assert _closed_popup(resp), resp.content[:2000]
    # Присутствие: человек заведён — отсутствие ниже про профиль, а не про него.
    assert User.objects.filter(username="drf2231-admin-empty").exists()
    assert _profiles("drf2231-admin-empty") == []  # empty-assert-ok: у admin сигнал профиль не создаёт


def test_admin_role_with_a_filled_block_gets_the_profile_from_the_block():
    client = _admin_client()

    resp = _add(
        client,
        username="drf2231-admin",
        role="admin",
        profile={"full_name": "Оператор", "city": "Казань"},
    )

    assert _closed_popup(resp), resp.content[:2000]
    profiles = _profiles("drf2231-admin")
    assert len(profiles) == 1
    assert profiles[0].full_name == "Оператор"


def test_a_master_still_has_exactly_one_specialist_profile():
    client = _admin_client()

    resp = _add(
        client,
        username="drf2231-master-sp",
        role="specialist",
        profile={"full_name": "Анна Петрова"},
    )

    assert _closed_popup(resp), resp.content[:2000]
    assert SpecialistProfile.objects.filter(user__username="drf2231-master-sp").count() == 1


def test_editing_an_existing_user_updates_the_profile_without_a_duplicate():
    client = _admin_client()
    user = User.objects.create_user(
        username="drf2231-existing", password=PASSWORD, role="specialist",
        email="existing-2231@example.com",
    )
    profile = Profile.objects.get(user=user)

    change_url = reverse("admin:users_user_change", args=[user.pk])
    page = client.get(change_url)
    assert page.status_code == 200
    data = _form_values(page)
    data.update({
        "profile-TOTAL_FORMS": "1",
        "profile-INITIAL_FORMS": "1",
        "profile-MIN_NUM_FORMS": "0",
        "profile-MAX_NUM_FORMS": "1",
        "profile-0-id": str(profile.pk),
        "profile-0-user": str(user.pk),
        "profile-0-full_name": "Мария Иванова",
        "profile-0-city": "Тверь",
        "profile-0-bio": "",
    })
    resp = client.post(change_url, data)

    assert resp.status_code == 302, resp.content[:2000]
    profiles = _profiles("drf2231-existing")
    assert len(profiles) == 1
    assert profiles[0].pk == profile.pk
    assert profiles[0].full_name == "Мария Иванова"


def _form_values(page) -> dict[str, str]:
    """Значения полей формы правки из контекста админки — чтобы POST правки
    нёс всё, что форма требует, а тест не зависел от их перечня."""
    form = page.context["adminform"].form
    values: dict[str, str] = {}
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                values[name] = "on"
            continue
        if isinstance(value, (list, tuple)):
            continue
        values[name] = str(value)
    return values
