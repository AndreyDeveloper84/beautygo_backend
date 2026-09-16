"""Место служебному тенанту не заводится молча — один предикат на двух вызывающих.

Замер 16.09.2026 (`Ayla/docs/MEASURE_SERVICELOCATION_PILOT_STATE_2026-09-16.md`):
в контуре одиннадцать тенантов, из них `ayla-marketplace` — не салон, а тенант
по умолчанию для новых пользователей приложения (`tenants/migrations/
0003_seed_default_tenants.py:36-39`). Различия в схеме нет: `Tenant.Kind` знает
только `SALON` и `SOLO`, умолчание — `SALON`.

Адрес непуст у всех одиннадцати, а единственная защита команды переноса —
непустой адрес. Значит `promote_tenant_location --slug ayla-marketplace --apply`
создаёт служебному тенанту место оказания услуг и не говорит ни слова.

Почему подтверждение, а не отказ
--------------------------------

У нового салона в день заведения мастеров тоже ноль, а команда нужна как раз
для новых салонов. Отказ закрыл бы законный путь. Поэтому условие — «у тенанта
ноль мастеров», а действие — **спросить**, и спрашивает каждый вызывающий
по-своему: команда требует флага, форма админки не проходит валидацию.

Почему предикат общий
---------------------

Место рождается в ДВУХ местах: инлайн на форме салона (`tenants/admin.py:55-65`,
шаг 2 операторского порядка) и команда (шаг 3, на готовом месте печатает «уже
есть» и выходит нулём). Сторож в одной команде оставил бы открытой ту дверь,
которой места заводят на самом деле. Условие живёт в одном месте; копия условия
в двух местах живёт до первого расхождения — сегодня это видно на
`participates_in_distance` и его ORM-двойнике `participating_place_q`.

Предикат несёт **условие**, а не действие: действия у вызывающих разные, и
затащи мы их внутрь — предикат не смог бы обслужить обоих.

Названный предел (§11.5 замера)
-------------------------------

Свойство «ноль мастеров» несёт сегодня ДВЕ защиты: эту и латентную правильность
`users/deletion_executor.py:732` / `users/personal_data_api.py:160`, которые
читают «не соло → салон». Привязка мастера к служебному тенанту через админку
снимает обе разом, и подтверждением это не лечится: вопрос задаётся при нуле
мастеров. Предел назван; снимает его только явный признак в схеме.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.contrib import admin as django_admin
from django.core.management import call_command
from django.test import RequestFactory

from tenants.models import ServiceLocation, Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

ADDRESS = "г. Пенза, ул. Кирова, д. 20"


def _run(**kw):
    out, err = StringIO(), StringIO()
    code = 0
    try:
        call_command("promote_tenant_location", stdout=out, stderr=err, **kw)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


def _inline_cls():
    for inline in django_admin.site._registry[Tenant].inlines:
        if inline.model is ServiceLocation:
            return inline
    return None


def _inline_form_cls(admin_user):
    """Форма инлайна так, как её строит Django, а не голый `ModelForm`.

    `TabularInline.form` по умолчанию — `forms.ModelForm` без `Meta.model`:
    создать его напрямую нельзя. `get_formset` собирает форму под модель —
    и до правки, и после, поэтому положительная половина зелена в обоих
    состояниях.

    `request` обязателен и обязан нести `user`: `get_formset` спрашивает
    права (`has_delete_permission` → `request.user.has_perm`), и на `None`
    падает `AttributeError` ещё до всякой валидации. Идиома — как в
    `services/tests/test_salon_service_admin.py:135-136`.
    """
    inline_cls = _inline_cls()
    assert inline_cls is not None, "инлайн мест пропал с формы салона"
    request = RequestFactory().post("/admin/tenants/tenant/add/")
    request.user = admin_user
    inline = inline_cls(Tenant, django_admin.site)
    return inline.get_formset(request).form


@pytest.fixture
def empty_tenant():
    """Служебный тенант: адрес есть, мастеров нет — ровно `ayla-marketplace`."""
    return Tenant.objects.create(slug="svc-tenant-2030", name="Ayla Marketplace",
                                 city="Пенза", address=ADDRESS)


@pytest.fixture
def salon_with_master():
    """Настоящий салон: адрес и хотя бы один мастер."""
    tenant = Tenant.objects.create(slug="salon-2030", name="Салон", city="Пенза",
                                   address=ADDRESS)
    user = User.objects.create_user(username="m-2030", password="x", phone="+79990002030")
    profile = SpecialistProfile._base_manager.filter(user=user).first()
    if profile is None:
        profile = SpecialistProfile._base_manager.create(user=user, display_name="Мастер")
    profile.tenant = tenant
    profile.save(update_fields=["tenant"])
    return tenant


@pytest.fixture
def admin_user():
    """Суперпользователь для `request.user`: `get_formset` спрашивает права."""
    return User.objects.create_superuser(username="adm-2030", password="x",
                                         email="adm2030@example.com")


# --- предикат ---------------------------------------------------------------

def test_predicate_says_yes_for_a_tenant_without_masters(empty_tenant):
    """Условие живёт одной функцией и отвечает про предмет, а не про имя."""
    from tenants.service_location import tenant_has_no_masters

    assert tenant_has_no_masters(empty_tenant) is True


def test_predicate_says_no_for_a_tenant_with_a_master(salon_with_master):
    """Положительная половина: иначе «да всегда» неотличимо от «да по делу»."""
    from tenants.service_location import tenant_has_no_masters

    assert tenant_has_no_masters(salon_with_master) is False


def test_the_predicate_distinguishes_rather_than_forbids(empty_tenant, salon_with_master):
    """Оба исхода одним прогоном и одной переменной — сторож различает."""
    from tenants.service_location import tenant_has_no_masters

    verdicts = {
        "служебный": tenant_has_no_masters(empty_tenant),
        "салон": tenant_has_no_masters(salon_with_master),
    }
    assert verdicts == {"служебный": True, "салон": False}, verdicts


# --- вызывающий 1: команда --------------------------------------------------

def test_command_does_not_create_a_place_for_a_tenant_without_masters(empty_tenant):
    """Единственная защита команды (непустой адрес) у служебного тенанта пройдена.

    Отказ обязан печатать ЧИСЛО: «мастеров 0» — иначе читатель не отличит
    отказ по делу от отказа по ошибке.
    """
    code, out, err = _run(slug=empty_tenant.slug, apply=True)

    assert code == 2, f"ожидал отказ, получил {code}; вывод: {out}"
    assert "мастеров" in (out + err) and "0" in (out + err)
    assert ServiceLocation.objects.filter(tenant=empty_tenant).count() == 0


def test_command_still_creates_a_place_for_a_salon_with_masters(salon_with_master):
    """Положительная половина: законный путь не закрыт. Зелено ДО и ПОСЛЕ правки."""
    code, out, _ = _run(slug=salon_with_master.slug, apply=True)

    assert code == 0, out
    assert ServiceLocation.objects.filter(tenant=salon_with_master).count() == 1


def test_the_flag_lets_an_empty_tenant_through_and_names_the_reason(empty_tenant):
    """Новый салон заводят раньше мастеров — путь остаётся, но становится решением."""
    code, out, _ = _run(slug=empty_tenant.slug, apply=True, no_masters_ok=True)

    assert code == 0, out
    assert "мастеров" in out
    assert ServiceLocation.objects.filter(tenant=empty_tenant).count() == 1


# --- вызывающий 2: инлайн админки -------------------------------------------

def test_inline_refuses_a_place_for_a_tenant_without_masters(empty_tenant, admin_user):
    """Дверь, которой места заводят на самом деле (шаг 2 операторского порядка)."""
    form_cls = _inline_form_cls(admin_user)
    form = form_cls(data={"address": ADDRESS, "city": "Пенза", "status": "review_required"},
                    instance=ServiceLocation(tenant=empty_tenant))

    assert form.is_valid() is False, "форма приняла место для тенанта без мастеров"
    assert "мастер" in str(form.errors).lower(), form.errors


def test_inline_accepts_a_place_for_a_salon_with_masters(salon_with_master, admin_user):
    """Положительная половина инлайна. Зелено ДО и ПОСЛЕ правки."""
    form_cls = _inline_form_cls(admin_user)
    form = form_cls(data={"address": ADDRESS, "city": "Пенза", "status": "review_required"},
                    instance=ServiceLocation(tenant=salon_with_master))

    assert form.is_valid() is True, form.errors


# --- то, ради чего выбрана область (3) --------------------------------------

def test_both_callers_read_the_same_predicate_not_a_copy():
    """Условие в одном месте, действие — у каждого своё.

    Сторож против будущей копии: впиши кто-нибудь счёт мастеров прямо в
    команду или в форму, условия разойдутся молча — ровно то, что уже
    случилось с `participates_in_distance` и `participating_place_q`, где
    совпадение удерживается только тестом.

    Сторож ловит **счёт**, а не упоминание: искать голое имя `SpecialistProfile`
    значило бы краснеть на соседнем комментарии (`tenants/admin.py:24` поминает
    `SpecialistProfileAdmin`, ничего не считая). Имя в прозе — не использование;
    ровно на этом сегодня оступился замер `SILENCED_SYSTEM_CHECKS`.
    """
    import inspect
    import re

    from tenants import admin as tenants_admin
    from tenants.management.commands import promote_tenant_location

    counting = re.compile(r"SpecialistProfile\s*\.\s*(_base_manager|objects)")
    for name, module in (("команда", promote_tenant_location), ("админка", tenants_admin)):
        src = inspect.getsource(module)
        assert "tenant_has_no_masters" in src, f"{name} не зовёт общий предикат"
        found = counting.findall(src)
        assert not found, f"{name} считает мастеров сама — это копия условия: {found}"
