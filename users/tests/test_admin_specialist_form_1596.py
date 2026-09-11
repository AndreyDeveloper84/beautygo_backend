"""Форма мастера в админке заводит мастера, которого видит клиент (DRF-1596).

Владелец 08.09.2026 открыл ``/admin/tenants/tenant/add/``, чтобы завести
настоящий салон с мастерами, и не нашёл, где добавить мастера. Он был прав
сразу в двух местах: на форме салона мастеров не было вовсе
(``TenantAdmin.inlines`` пуст — это закрыто в ``tenants/tests``), а на
форме мастера не было салона.

Здесь закрепляется вторая половина.

Замер до правки (``SpecialistProfileAdmin.get_form(None).base_fields``)::

    address avatar bio display_name experience_years is_available
    location_lat location_lng status user

То есть из десяти полей модели с ``blank=False`` — ``user``,
``display_name``, ``experience_years``, ``timezone``,
``is_booking_enabled``, ``status``, ``rating``, ``reviews_count``,
``is_available``, ``booking_source`` — в форме отсутствовали ПЯТЬ:
``timezone``, ``is_booking_enabled``, ``booking_source``, ``rating``,
``reviews_count``. Последние два стоят в наборе полей как
``readonly_fields``, то есть на экране их видно, и это осознанно: их
считает платформа, а не оператор. Три остальных не было ни в форме, ни на
экране — они подставлялись молчаливыми умолчаниями модели. Плюс шестым
отсутствовал ``tenant`` (``blank=True``, но именно он решает, к какому
салону мастер принадлежит и попадёт ли он в ``?tenant=``-выборку зеркала).

Критерий приёмки тикета — не «форма сохраняет», а «мастера видно
клиенту»: строка, заведённая формой, обязана доехать до
``GET /api/v1/internal/specialists/``. Последний тест модуля проверяет
именно это, и проверяет он реальным POST в админку, а не ORM-фикстурой —
иначе доказывается не форма.
"""
from __future__ import annotations

import pytest
from django.contrib import admin as django_admin
from django.test import RequestFactory
from django.urls import reverse
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-ayla-internal-token-1596"
INTERNAL_URL = "/api/v1/internal/specialists/"

#: Поля модели с ``blank=False``: ORM их не заполнит из пользовательского
#: ввода, значит без явного вывода в форму каждое из них подставится
#: умолчанием, о котором оператор не узнает.
REQUIRED_MODEL_FIELDS = {
    "user", "display_name", "experience_years", "timezone",
    "is_booking_enabled", "status", "rating", "reviews_count",
    "is_available", "booking_source",
}

#: Из них платформа считает сама — оператор их не вводит, и это причина,
#: а не недосмотр: ``rating``/``reviews_count`` пишет агрегация отзывов.
COMPUTED_BY_PLATFORM = {"rating", "reviews_count"}


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


def _admin_form_fields() -> set[str]:
    """Поля формы мастера так, как их собирает сама админка.

    Форма строится под запрос: виджеты связей спрашивают права
    пользователя (кнопки «добавить/изменить» рядом с полем), поэтому
    ``get_form(None)`` падает. Запрос настоящий — с суперпользователем,
    то есть замер снимается в самых широких правах: если поля нет здесь,
    его нет ни у кого.
    """
    model_admin = django_admin.site._registry[SpecialistProfile]
    request = RequestFactory().get("/admin/users/specialistprofile/add/")
    request.user = User.objects.create_superuser(
        username="drf1596-form-probe", password="pw",  # pragma: allowlist secret
        email="p@b.c",
        role="admin",
    )
    return set(model_admin.get_form(request).base_fields)


def _shown_on_screen() -> set[str]:
    """Всё, что оператор видит на форме: поля ввода + readonly-поля."""
    model_admin = django_admin.site._registry[SpecialistProfile]
    shown: set[str] = set()
    for _title, opts in model_admin.fieldsets:
        shown.update(opts["fields"])
    return shown


def test_tenant_is_editable_on_the_master_form():
    """Салон мастера заводится формой мастера.

    ``SpecialistProfile.tenant`` есть в модели с DRF-242.3, но в форму
    выведен не был: мастер, заведённый админкой, оставался без салона.
    Такой мастер невидим для зеркала бота — оно тянет каталог с
    ``?tenant=<uuid>`` (DRF-1313), и строка с ``tenant IS NULL`` не
    попадает ни в одну пер-салонную выборку.
    """
    assert "tenant" in _admin_form_fields()


def test_every_required_field_is_on_the_form():
    """Ни одно обязательное поле не подставляется молча.

    Поля, которые считает платформа, разрешено держать readonly — но
    видимыми: оператор должен понимать, откуда взялось значение.
    """
    form_fields = _admin_form_fields()
    on_screen = _shown_on_screen()

    editable_required = REQUIRED_MODEL_FIELDS - COMPUTED_BY_PLATFORM
    assert editable_required <= form_fields, (
        "не выведены в форму: " + ", ".join(sorted(editable_required - form_fields))
    )
    assert COMPUTED_BY_PLATFORM <= on_screen, (
        "не видно на экране: " + ", ".join(sorted(COMPUTED_BY_PLATFORM - on_screen))
    )


def test_decisive_fields_carry_an_explanation():
    """У полей, решающих судьбу мастера, есть подсказка.

    Образец — раздел «Адрес» в ``TenantAdmin`` (DRF-1587): он объясняет,
    что пустое поле уезжает как ``null`` и салон без города не попадает в
    городской поиск. Здесь то же самое требуется от разделов, где стоят
    ``status`` / ``is_available`` / ``is_booking_enabled`` /
    ``booking_source`` / ``timezone`` / ``tenant``.
    """
    model_admin = django_admin.site._registry[SpecialistProfile]
    decisive = {
        "tenant", "status", "is_available", "is_booking_enabled",
        "booking_source", "timezone",
    }
    explained: set[str] = set()
    for _title, opts in model_admin.fieldsets:
        if (opts.get("description") or "").strip():
            explained.update(opts["fields"])
    assert decisive <= explained, (
        "без подсказки: " + ", ".join(sorted(decisive - explained))
    )


@pytest.mark.no_auto_tenant
def test_master_created_through_the_form_reaches_the_client_feed(client):
    """Сквозной путь: салон + пользователь + профиль формой → каталог.

    Всё заводится POST-ами в админку — то есть ровно тем путём, которым
    это делает владелец. Никакой ORM-подготовки объектов, кроме учётки
    администратора: иначе тест доказывал бы модель, а не форму.
    """
    staff = User.objects.create_superuser(
        username="drf1596-admin", password="pw",  # pragma: allowlist secret
        email="a@b.c", role="admin",
    )
    client.force_login(staff)

    # 1. Салон — формой салона.
    resp = client.post(
        reverse("admin:tenants_tenant_add"),
        {
            "slug": "drf1596-salon",
            "name": "Салон DRF-1596",
            "city": "Пенза",
            "address": "Пенза, ул. Московская, 1",
            "is_active": "on",
            # management-формы вложенного блока мастеров
            "specialist_profiles-TOTAL_FORMS": "0",
            "specialist_profiles-INITIAL_FORMS": "0",
            "specialist_profiles-MIN_NUM_FORMS": "0",
            "specialist_profiles-MAX_NUM_FORMS": "1000",
            # L1 DRF-1687: у салона появился второй вложенный блок — места
            # оказания услуг; браузер шлёт его management-форму всегда.
            "locations-TOTAL_FORMS": "0",
            "locations-INITIAL_FORMS": "0",
            "locations-MIN_NUM_FORMS": "0",
            "locations-MAX_NUM_FORMS": "1000",
        },
    )
    assert resp.status_code == 302, resp.status_code
    tenant = Tenant.all_objects.get(slug="drf1596-salon")

    # 2. Пользователь-мастер — формой пользователя. Сигнал
    #    ``create_user_profile`` заводит вместе с ним SpecialistProfile
    #    (tenant=None, status=draft) — то есть невидимого мастера.
    resp = client.post(
        reverse("admin:users_user_add"),
        {
            "username": "drf1596-master",
            "password1": "Sup3rSecret!42",  # pragma: allowlist secret
            "password2": "Sup3rSecret!42",  # pragma: allowlist secret
            "role": "specialist",
            "phone": "+79001596001",
            # management-формы блока «Профиль клиента» на форме
            # пользователя — он там был и до тикета.
            "profile-TOTAL_FORMS": "0",
            "profile-INITIAL_FORMS": "0",
            "profile-MIN_NUM_FORMS": "0",
            "profile-MAX_NUM_FORMS": "1",
        },
    )
    assert resp.status_code == 302, resp.content[:2000]
    master_user = User.objects.get(username="drf1596-master")
    profile = SpecialistProfile.objects.get(user=master_user)
    assert profile.status == SpecialistProfile.ProfileStatus.DRAFT

    # 3. Профиль мастера — формой мастера. Именно здесь оператор
    #    обязан иметь возможность выставить салон и всё, что решает
    #    видимость.
    resp = client.post(
        reverse("admin:users_specialistprofile_change", args=[profile.pk]),
        {
            "tenant": str(tenant.id),
            "user": str(master_user.id),
            "display_name": "Мария",
            "bio": "",
            "status": SpecialistProfile.ProfileStatus.ACTIVE,
            "is_available": "on",
            "is_booking_enabled": "on",
            "timezone": "Europe/Moscow",
            "booking_source": SpecialistProfile.BookingSource.AYLA_LOCAL,
            "experience_years": "3",
            "address": "",
            "location_lat": "",
            "location_lng": "",
            # management-формы вложенного блока расписания. Блок появился
            # вместе с ним же: часы мастера переехали на его форму, чтобы
            # завести салон из пяти мастеров стоило пять отправок, а не
            # тридцать пять. Без этих ключей Django отвергает весь POST —
            # форма возвращается страницей с ошибкой, а не редиректом, и
            # тест падает не на предмете, а на неполном запросе.
            "working_hours-TOTAL_FORMS": "0",
            "working_hours-INITIAL_FORMS": "0",
            "working_hours-MIN_NUM_FORMS": "0",
            "working_hours-MAX_NUM_FORMS": "1000",
        },
    )
    assert resp.status_code == 302, resp.content[:4000]

    profile.refresh_from_db()
    assert profile.tenant_id == tenant.id
    assert profile.status == SpecialistProfile.ProfileStatus.ACTIVE

    # 4. Мастер обязан доехать до клиента — и именно в выборке своего
    #    салона, потому что зеркало бота ходит только так (DRF-1313).
    api = APIClient()
    api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    body = api.get(INTERNAL_URL, {"tenant": str(tenant.id)}).json()
    rows = body.get("results", body) if isinstance(body, dict) else body
    assert [r["display_name"] for r in rows] == ["Мария"]
    # ``user_id`` — мост до ``CatalogMaster.ayla_user_id``; без него
    # гейт продажи в зеркале (``AVAILABLE``) мастера не пропустит.
    assert rows[0]["user_id"] == str(master_user.id)
