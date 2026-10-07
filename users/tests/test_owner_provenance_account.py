"""Именная учётка владельца для провенанса (решение владельца 07.10).

Миграция ``0031`` схему не меняет — это данные; узлы зовут шаг напрямую на
живой модели. ``--create-db`` уже накатил ``0031``, поэтому учётка в базе
узла есть с самого начала: узлы, которым нужна чистая база, её удаляют.

Узлы держат:

* учётка заводится с фиксированным pk и именем владельца;
* войти ею нельзя и прав у неё нет;
* повторный прогон ничего не меняет;
* учётку, заведённую раньше руками, шаг не правит, а ссылка по ключу
  находит её по её собственному pk;
* ключ, занятый внешней, гостевой или тестовой личностью, — отказ, и
  авторство такой учётке не приписывается;
* выключенной учётке авторство не приписывается.
"""

from __future__ import annotations

import importlib
import uuid

import pytest
from django.apps import apps as live_apps
from django.contrib.auth.hashers import make_password

from users.migrations import _owner_provenance_account as owner_account
from users.models import User

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("users.migrations.0031_owner_provenance_account")


@pytest.fixture
def clean(db):
    User.objects.filter(username=owner_account.USERNAME).delete()


def _ensure():
    return owner_account.ensure(User, unusable_password=make_password(None))


def test_the_migration_has_already_made_the_account() -> None:
    account = User.objects.get(username=owner_account.USERNAME)

    assert str(account.pk) == owner_account.FIXED_ID


def test_the_account_is_made_with_the_fixed_pk_and_the_owner_name(clean) -> None:
    pk, created = _ensure()

    account = User.objects.get(username=owner_account.USERNAME)
    assert created is True
    assert str(pk) == str(account.pk) == owner_account.FIXED_ID
    assert (account.first_name, account.last_name) == ("Андрей", "Тихонов")


def test_the_account_cannot_log_in_and_has_no_rights(clean) -> None:
    _ensure()

    account = User.objects.get(username=owner_account.USERNAME)
    assert not account.has_usable_password()
    assert (account.phone, account.email, account.tenant_id) == (None, "", None)
    assert not any(
        (account.is_staff, account.is_superuser, account.is_platform_admin,
         account.is_proxy, account.is_guest, account.is_test_persona)
    )
    assert account.is_active is True


def test_a_second_run_changes_nothing(clean) -> None:
    _ensure()
    before = User.objects.filter(username=owner_account.USERNAME).values().get()

    pk, created = _ensure()
    MIGRATION.ensure_account(live_apps, None)

    assert created is False
    assert str(pk) == owner_account.FIXED_ID
    assert User.objects.filter(username=owner_account.USERNAME).values().get() == before


def test_an_account_made_by_hand_is_left_as_it_is(clean) -> None:
    by_hand = User.objects.create_user(
        username=owner_account.USERNAME, password="x", role="admin", is_staff=True
    )
    before = User.objects.filter(pk=by_hand.pk).values().get()

    pk, created = _ensure()

    assert (pk, created) == (by_hand.pk, False)
    assert User.objects.filter(pk=by_hand.pk).values().get() == before
    assert owner_account.account_id(User) == by_hand.pk
    assert str(by_hand.pk) != owner_account.FIXED_ID


@pytest.mark.parametrize("flag", ["is_proxy", "is_guest", "is_test_persona"])
def test_a_key_held_by_someone_who_is_not_the_owner_is_refused(clean, flag) -> None:
    User.objects.create_user(username=owner_account.USERNAME, password="x", **{flag: True})

    with pytest.raises(owner_account.NotAProvenanceAccount):
        _ensure()

    assert owner_account.account_id(User) is None


def test_a_switched_off_account_is_not_an_author(clean) -> None:
    _ensure()
    assert str(owner_account.account_id(User)) == owner_account.FIXED_ID

    User.objects.filter(username=owner_account.USERNAME).update(is_active=False)

    assert owner_account.account_id(User) is None


def test_no_account_means_no_author(clean) -> None:
    assert owner_account.account_id(User) is None


def test_the_literals_match_the_model() -> None:
    assert owner_account.ROLE in dict(User.ROLE_CHOICES)
    assert str(uuid.UUID(owner_account.FIXED_ID)) == owner_account.FIXED_ID
    assert owner_account.FIXED_ID == str(
        uuid.uuid5(uuid.NAMESPACE_URL, "ayla:provenance:" + owner_account.USERNAME)
    )
    User._meta.get_field("username").run_validators(owner_account.USERNAME)
