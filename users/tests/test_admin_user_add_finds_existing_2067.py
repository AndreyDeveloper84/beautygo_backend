"""Форма «добавить пользователя» находит уже заведённого человека (DRF-2067).

Аудит ``docs/IDENTITY_AUDIT_CATALOG_2026-09-16.md`` §г: на
``/admin/users/user/add/`` стоит штатная ``BaseUserAdmin`` и никакой
проверки на дубль человека нет. Единственная защита — ``User.phone``
``unique=True``, но ``null=True`` — и потому человек **без телефона**
заводится повторно сколько угодно раз: ``NULL ≠ NULL``.

Красное до правки, измерено на этом же модуле (``9bcc6724``, локально):
второй ``User`` без телефона с тем же email — форма отвечает 302 и в базе
две строки на одного человека.

Что закрепляется:

* поиск существующего **по набору** — телефон И email (email без учёта
  регистра); совпадение по любому — отказ, который называет найденного
  человека и предлагает **связать**, а не создать;
* пустой телефон **назван**: без телефона обязателен email — иначе
  человека не с чем сравнивать, и дыра остаётся по построению;
* положительная стража — человек с новыми телефоном и email заводится,
  как и раньше; человек без телефона, но с новым email — тоже.

Коды здесь настоящие для админки: успех формы — 302 на список, отказ —
200 с формой и ошибкой; «201/400» из постановки — это их смысл, не числа.
"""
from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from users.models import User

pytestmark = pytest.mark.django_db

ADD_URL_NAME = "admin:users_user_add"
PASSWORD = "Sup3rSecret!42"  # pragma: allowlist secret


def _admin_client() -> Client:
    staff = User.objects.create_superuser(
        username="drf2067-operator",
        email="operator-2067@example.com",
        password=PASSWORD,
        role="admin",
    )
    client = Client()
    client.force_login(staff)
    return client


def _post_add(client: Client, **fields: str):
    """POST формы добавления с management-формой блока «Профиль клиента»."""
    data = {
        "username": fields.pop("username"),
        "password1": PASSWORD,
        "password2": PASSWORD,
        "role": fields.pop("role", "specialist"),
        "phone": fields.pop("phone", ""),
        "email": fields.pop("email", ""),
        "profile-TOTAL_FORMS": "0",
        "profile-INITIAL_FORMS": "0",
        "profile-MIN_NUM_FORMS": "0",
        "profile-MAX_NUM_FORMS": "1",
    }
    assert not fields, fields
    return client.post(reverse(ADD_URL_NAME), data)


def _existing(**kwargs) -> User:
    return User.objects.create_user(
        username=kwargs.pop("username", "drf2067-existing"),
        password=PASSWORD,
        role=kwargs.pop("role", "specialist"),
        **kwargs,
    )


def _people() -> int:
    """Люди, а не строки: оператор-суперпользователь из фикстуры не в счёт."""
    return User.objects.exclude(username="drf2067-operator").count()


# ─── Дыра из аудита §г ────────────────────────────────────────────────────────

def test_second_user_without_phone_with_same_email_is_refused():
    """NULL ≠ NULL: без телефона DB-уникальность молчит, ловит только форма."""
    client = _admin_client()
    existing = _existing(phone=None, email="masha@example.com")

    resp = _post_add(client, username="drf2067-second", email="masha@example.com")

    assert resp.status_code == 200, resp.status_code
    assert _people() == 1
    body = resp.content.decode()
    assert existing.username in body
    # Ссылка живая, не экранированный текст: иначе «предложить связать»
    # было бы прозой без хода.
    url = reverse("admin:users_user_change", args=[existing.pk])
    assert f'href="{url}"' in body


def test_email_match_ignores_case():
    client = _admin_client()
    _existing(phone=None, email="Masha@Example.com")

    resp = _post_add(client, username="drf2067-second", email="masha@example.com")

    assert resp.status_code == 200
    assert _people() == 1


def test_same_phone_names_the_existing_person_not_a_bare_unique_error():
    """Телефон и до правки не проходил (unique), но отказ не называл
    человека и не предлагал связать — теперь называет."""
    client = _admin_client()
    existing = _existing(phone="+79002067001", email="one@example.com")

    resp = _post_add(
        client, username="drf2067-second", phone="+79002067001",
        email="two@example.com",
    )

    assert resp.status_code == 200
    assert _people() == 1
    url = reverse("admin:users_user_change", args=[existing.pk])
    assert f'href="{url}"' in resp.content.decode()


def test_soft_deleted_person_still_counts_as_existing():
    """Удалённый человек — всё ещё тот же человек: заводить второго нельзя,
    отказ называет состояние, чтобы оператор шёл восстанавливать, а не
    дублировать."""
    from django.utils import timezone

    client = _admin_client()
    existing = _existing(phone=None, email="gone@example.com", is_active=False)
    existing.deleted_at = timezone.now()
    existing.save(update_fields=["deleted_at"])

    resp = _post_add(client, username="drf2067-second", email="gone@example.com")

    assert resp.status_code == 200
    assert _people() == 1


# ─── Пустой телефон назван ───────────────────────────────────────────────────

def test_neither_phone_nor_email_is_refused_by_construction():
    client = _admin_client()

    resp = _post_add(client, username="drf2067-nobody")

    assert resp.status_code == 200
    assert _people() == 0


# ─── Положительная стража ────────────────────────────────────────────────────

def test_new_phone_and_new_email_is_created():
    client = _admin_client()
    _existing(phone="+79002067001", email="one@example.com")

    resp = _post_add(
        client, username="drf2067-new", phone="+79002067002",
        email="two@example.com",
    )

    assert resp.status_code == 302, resp.content[:2000]
    assert _people() == 2
    assert User.objects.get(username="drf2067-new").email == "two@example.com"


def test_no_phone_but_new_email_is_created():
    """Пустой телефон — не отказ сам по себе: с email человек различим."""
    client = _admin_client()
    _existing(phone=None, email="one@example.com")

    resp = _post_add(client, username="drf2067-new", email="two@example.com")

    assert resp.status_code == 302, resp.content[:2000]
    assert _people() == 2


def test_email_is_on_the_add_form():
    """Без email на форме добавления искать по нему не по чему — поле
    обязано быть на самой форме, а не только на форме изменения."""
    from django.contrib import admin as django_admin

    model_admin = django_admin.site._registry[User]
    fields = {
        f for _, opts in model_admin.get_fieldsets(None, None)
        for f in opts["fields"]
    }
    assert "email" in fields
    assert "phone" in fields
