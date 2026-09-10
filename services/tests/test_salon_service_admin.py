"""Поверхность разметки: оператор видит статус связи и понимает отказ (§93).

Решение владельца §93 требует ручного разбора услуг пилотного салона —
пятьдесят шесть решений, каждое из трёх исходов: связать с существующим
каноном, создать недостающий, отметить не подлежащей рекомендациям.

Замер 10.09.2026 показал, что делать это негде.

**Первое: статуса нет на экране.** Он есть в модели с §76, но::

    SalonServiceAdmin.list_display
        name tenant template category duration_minutes
        requires_health_check is_active source
    SalonServiceAdmin.list_filter
        is_active source requires_health_check tenant

``mapping_status`` не в списке и не в фильтрах. Оператор не может ни
увидеть статус строки, ни отобрать непроверенные — а выборка «покажи всё,
что ждёт проверки» и есть его рабочий инструмент. Индекс под неё в модели
уже стоит (``salonsvc_tenant_mapstatus_idx``), и пользоваться им некому.

**Второе: отказ непонятен.** Здесь первая редакция замера ошиблась, и
ошибка снимается этим файлом. Я записал, что оператор получает
``IntegrityError`` и страницу 500, потому что инвариант происхождения
живёт только в ``CheckConstraint``. Это неверно: Django с 4.1 проверяет
ограничения модели в ``full_clean()``, а ``ModelForm._post_clean`` его
вызывает — форма отказ **даёт**, до базы строка не доходит. Замерено::

    is_valid()  ->  False
    errors      ->  {'__all__': ['Нарушено ограничение
                     "salonservice_verified_requires_provenance".']}

Но прочитать это оператор не может. Сообщение висит на форме целиком, а
не на поле, и называет **имя ограничения базы** вместо того, чего не
хватает. Человек, размечающий пятьдесят шесть строк, узнаёт из него, что
он что-то нарушил, и не узнаёт что.

**Зачем сторож в форме, если ограничение уже есть в базе.** Не ради
симметрии: у двух сторожей разные адресаты и разные ответы. База держит
истину и не обходится ни ``update()``, ни миграцией данных — она отвечает
системе словом «нельзя». Форма отвечает человеку и обязана сказать, чего
именно не хватает и в каком поле. Снять любой из них нельзя: без базы
инвариант обходится кодом, без формы — непониманием.

**Побочно найденное, чинится не здесь.** Докстринг модели называет
``mapping_confirmed_by`` и ``mapping_confirmed_rule``
взаимоисключающими («владелец назвал кто ИЛИ какое правило»), а
``CheckConstraint`` написан через ``OR`` и оба заполненных **пропускает**.
Документированный инвариант без сторожа. Форма закрывает его здесь;
базе это чинится в ``services/models.py``, который занят PR #307.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib import admin as django_admin
from django.test import RequestFactory

from services.models import SalonService, ServiceCategory
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

S = SalonService.MappingStatus

#: Текст, который оператор видел до правки. Тест на понятность обязан
#: краснеть именно на нём, иначе он проверяет «есть хоть какая-то ошибка».
DB_CONSTRAINT_WORDING = "salonservice_verified_requires_provenance"


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="admin-surface-tenant", name="Салон разметки")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Массаж тела", slug="admin-surface-massage")


@pytest.fixture
def human(db):
    return User.objects.create_user(username="admin-surface-human", password="x")


def _model_admin():
    return django_admin.site._registry[SalonService]


def _form_data(tenant, category, **overrides) -> dict:
    """Полный набор полей формы так, как их присылает браузер.

    ``mapping_confirmed_at`` подаётся ДВУМЯ ключами: админка рисует его
    виджетом ``AdminSplitDateTime`` (дата и время раздельно). Одним
    ключом значение молча не парсится и приезжает ``None`` — то есть
    тест «валидная строка проходит» падал бы на собственной фикстуре и
    доказывал бы несуществующий дефект формы. Проверено: так и было.
    """
    data = {
        "tenant": str(tenant.pk),
        "template": "",
        "category": str(category.pk),
        "name": "Классический массаж",
        "duration_minutes": "60",
        "base_price": "2500",
        "is_active": True,
        "source": SalonService.Source.MANUAL,
        "mapping_status": S.UNMAPPED,
        "mapping_confirmed_by": "",
        "mapping_confirmed_rule": "",
        "mapping_rule_version": "",
        "mapping_confirmed_at_0": "",
        "mapping_confirmed_at_1": "",
        "mapping_source_ref": "",
    }
    data.update(overrides)
    return data


def _confirmed_at(date="2026-09-10", time="12:00:00") -> dict:
    return {"mapping_confirmed_at_0": date, "mapping_confirmed_at_1": time}


def _bound_form(tenant, category, **overrides):
    """Форма так, как её собирает сама админка, под настоящим запросом.

    ``get_form(None)`` здесь не годится и падает: виджеты полей-связей
    спрашивают права у ``request.user``, чтобы решить, рисовать ли рядом
    кнопки «добавить/изменить». Пользователь — суперпользователь, то есть
    замер снимается в самых широких правах: если форма отвергает строку
    здесь, она отвергнет её у кого угодно.
    """
    request = RequestFactory().post("/admin/services/salonservice/add/")
    request.user = User.objects.create_superuser(
        username=f"admin-surface-probe-{uuid.uuid4().hex[:8]}",
        password="pw",  # pragma: allowlist secret
        email="probe@example.com",
        role="admin",
    )
    form_class = _model_admin().get_form(request, obj=None, change=False)
    return form_class(data=_form_data(tenant, category, **overrides))


def _field_errors(form) -> dict[str, list[str]]:
    """Ошибки, привязанные к полям. ``__all__`` — не поле, он исключён."""
    form.is_valid()
    return {k: [str(m) for m in v] for k, v in form.errors.items() if k != "__all__"}


# ---------------------------------------------------------------------------
# Видно ли статус
# ---------------------------------------------------------------------------


def test_mapping_status_is_in_the_list():
    """Статус связи видно в списке услуг.

    Без него оператор, открывший список, не отличит подтверждённую связь
    от непроверенной — а разбор пятидесяти шести услуг состоит ровно из
    этого различения.
    """
    assert "mapping_status" in _model_admin().list_display


def test_mapping_status_is_filterable():
    """Очередь проверки собирается фильтром, а не глазами.

    Индекс ``(tenant, mapping_status)`` в модели заведён под эту самую
    выборку (``salonsvc_tenant_mapstatus_idx``). Фильтра нет — индексом
    никто не пользуется, и «покажи непроверенные» делается перебором.
    """
    assert "mapping_status" in _model_admin().list_filter


def test_provenance_is_visible_next_to_the_status():
    """Рядом со статусом видно, когда связь подтверждена.

    Статус без происхождения — это §73: имя, которое читается как факт.
    Оператор, глядящий на ``verified``, должен видеть в той же строке,
    что за ним что-то стоит, а не верить слову.
    """
    shown = set(_model_admin().list_display)
    assert "mapping_confirmed_at" in shown, (
        "рядом со статусом не видно происхождения: " + ", ".join(sorted(shown))
    )


# ---------------------------------------------------------------------------
# Отказ, который можно прочитать
# ---------------------------------------------------------------------------


def test_missing_source_ref_is_named_on_its_own_field(tenant, category, human):
    """Не хватает основания — сказано в поле основания.

    До правки эта строка отвергалась сообщением
    ``Нарушено ограничение "salonservice_verified_requires_provenance"``
    на форме целиком. Отказ был, инструкции не было.
    """
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=str(human.pk),
        mapping_source_ref="",
        **_confirmed_at(),
    )
    errors = _field_errors(form)
    assert "mapping_source_ref" in errors, f"ошибка не привязана к полю: {errors}"
    assert DB_CONSTRAINT_WORDING not in " ".join(errors["mapping_source_ref"]), (
        "оператору показано имя ограничения базы вместо объяснения"
    )


def test_missing_confirmed_at_is_named_on_its_own_field(tenant, category, human):
    """Не хватает даты подтверждения — сказано в поле даты."""
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=str(human.pk),
        mapping_source_ref="разбор 56 услуг, строка 12",
    )
    assert "mapping_confirmed_at" in _field_errors(form)


def test_missing_who_and_rule_is_named(tenant, category):
    """Не сказано ни кто, ни каким правилом — сказано, что нужно одно из.

    ``VERIFIED`` без человека и без правила — статус без происхождения,
    то самое, что §76 запретил.
    """
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_source_ref="разбор 56 услуг, строка 12",
        **_confirmed_at(),
    )
    assert "mapping_confirmed_by" in _field_errors(form)


def test_rule_without_version_is_named_on_the_version_field(tenant, category):
    """Правило без версии — «подтверждено какой-то из версий».

    Второй ``CheckConstraint`` модели говорит это базе. Человеку должна
    сказать форма, и сказать в поле версии.
    """
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_confirmed_rule="exact_name_match",
        mapping_rule_version="",
        mapping_source_ref="DRF-1533",
        **_confirmed_at(),
    )
    assert "mapping_rule_version" in _field_errors(form)


def test_both_who_and_rule_is_refused(tenant, category, human):
    """Кто ИЛИ правило, но не оба сразу.

    Докстринг модели называет поля взаимоисключающими, а
    ``CheckConstraint`` написан через ``OR`` и оба заполненных
    пропускает — документированный инвариант без сторожа. Пока база не
    поправлена (``services/models.py`` занят PR #307), сторожем работает
    форма.
    """
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=str(human.pk),
        mapping_confirmed_rule="exact_name_match",
        mapping_rule_version="v1",
        mapping_source_ref="DRF-1533",
        **_confirmed_at(),
    )
    assert not form.is_valid(), "форма приняла и человека, и правило сразу"


# ---------------------------------------------------------------------------
# Положительная стража: сторож, запрещающий всё, ничего не доказывает
# ---------------------------------------------------------------------------


def test_verified_with_human_provenance_passes(tenant, category, human):
    """Правильно заполненное подтверждение человеком форма пропускает."""
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=str(human.pk),
        mapping_source_ref="разбор 56 услуг, строка 12",
        **_confirmed_at(),
    )
    assert form.is_valid(), form.errors.as_text()


def test_verified_with_rule_provenance_passes(tenant, category):
    """И подтверждение детерминированным правилом с версией — тоже."""
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_confirmed_rule="exact_name_match",
        mapping_rule_version="v1",
        mapping_source_ref="DRF-1533",
        **_confirmed_at(),
    )
    assert form.is_valid(), form.errors.as_text()


def test_unmapped_without_provenance_passes(tenant, category):
    """Непроверенная связь не обязана нести происхождение.

    Инвариант касается только ``verified``. Требуй форма происхождение у
    каждой строки — обычную услугу стало бы не завести, а услуг без
    связи на пилоте пятьдесят девять.
    """
    assert _bound_form(tenant, category).is_valid(), "форма отвергла обычную услугу"


def test_saved_row_reaches_the_database(tenant, category, human):
    """Строка, принятая формой, доезжает до базы и не роняет ограничение.

    Валидная форма — ещё не доказательство: ``CheckConstraint`` живёт в
    схеме и разойтись с формой может молча. Проверяется сохранением, и
    считается ``== before + 1``, а не ``>=``: ноль обязан иметь
    положительную стражу.
    """
    form = _bound_form(
        tenant, category,
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=str(human.pk),
        mapping_source_ref="разбор 56 услуг, строка 12",
        **_confirmed_at(),
    )
    assert form.is_valid(), form.errors.as_text()
    before = SalonService.objects.filter(mapping_status=S.VERIFIED).count()
    form.save()
    assert SalonService.objects.filter(mapping_status=S.VERIFIED).count() == before + 1


# ---------------------------------------------------------------------------
# Третий исход §93 — отказ читается так же, как подтверждение
# ---------------------------------------------------------------------------


def test_refusal_without_source_ref_is_named_on_its_own_field(tenant, category, human):
    """Отказ без основания объясняется полем, а не именем ограничения.

    Когда §93 добавил четвёртое состояние, сравнение `!= VERIFIED` в
    форме молча перестало покрывать половину случаев: подтверждение
    получало человеческое сообщение, а отказ — `IntegrityError` от
    `salonservice_not_recommendable_requires_provenance`, то есть ровно
    то, что этот класс и чинил.

    Отказ — такое же решение, и объясняться должен так же.
    """
    form = _bound_form(
        tenant, category,
        mapping_status=S.NOT_RECOMMENDABLE,
        mapping_confirmed_by=str(human.pk),
        mapping_source_ref="",
        **_confirmed_at(),
    )
    errors = _field_errors(form)
    assert "mapping_source_ref" in errors, f"ошибка не привязана к полю: {errors}"
    assert "not_recommendable_requires_provenance" not in " ".join(
        errors["mapping_source_ref"]
    ), "оператору показано имя ограничения базы вместо объяснения"


def test_refusal_without_who_and_rule_is_named(tenant, category):
    """Отказ без автора и без правила — то же требование, что у связи."""
    form = _bound_form(
        tenant, category,
        mapping_status=S.NOT_RECOMMENDABLE,
        mapping_source_ref="разбор 56 услуг, строка 41",
        **_confirmed_at(),
    )
    assert "mapping_confirmed_by" in _field_errors(form)


def test_properly_recorded_refusal_passes(tenant, category, human):
    """Положительная стража: оформленный отказ форма пропускает.

    Без неё две проверки выше зеленели бы и на форме, которая отвергает
    четвёртое состояние целиком, — а тогда разбор 56 услуг снова было
    бы некуда записывать.
    """
    form = _bound_form(
        tenant, category,
        mapping_status=S.NOT_RECOMMENDABLE,
        mapping_confirmed_by=str(human.pk),
        mapping_source_ref="разбор 56 услуг, строка 41: это не процедура",
        **_confirmed_at(),
    )
    assert form.is_valid(), form.errors.as_text()
