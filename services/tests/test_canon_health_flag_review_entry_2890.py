"""DRF-2890 — вход куратора для просмотра флага проверки здоровья у канона.

Перепись на пилоте (DRF-2877, 08.10): после правила владельца из пакета S2
запись не проходит ни по одному предложению — ни один флаг «нужна ли
проверка перед услугой» у канона человеком не просмотрен. Просмотреть флаг
через форму канона можно было и раньше, но вслепую: ни очереди, ни проверки
по полю, автор и дата — руками.

Это инструмент для разметки, не разметка. Узлы держат:

* в списке канонов видно значение флага и его происхождение, и по
  происхождению список фильтруется — очередь «не просмотрен» строится;
* подтверждённый флаг требует основания, автора ИЛИ правила (не обоих),
  правило — с версией; отказ — по полю, не именем ограничения;
* просмотр без названного автора подписывает тот, кто сохраняет, моментом
  сохранения; названного автора, правило и стоящую дату это не трогает;
  основание не подставляется никогда;
* черновой флаг сохраняется без подписи;
* флаг, изменённый под прежним просмотром, форма не пропускает молча; с
  новым просмотром — пропускает;
* массового действия «подтвердить» нет.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.contrib import admin as django_admin
from django.forms.models import model_to_dict
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from services.admin import ServiceTemplateAdminForm
from services.models import ServiceCategory, ServiceTemplate
from users.models import User

pytestmark = pytest.mark.django_db

Origin = ServiceTemplate.HealthCheckOrigin
REVIEWED_AT = timezone.now().replace(microsecond=0) - timedelta(days=4)


@pytest.fixture
def curator(db):
    return User.objects.create_user(
        username="curator-2890", password="x", is_staff=True, is_superuser=True,
    )


@pytest.fixture
def another(db):
    return User.objects.create_user(username="another-2890", password="x", is_staff=True)


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Просмотр 2890", slug="review-2890")


def _canon(category, name, *, flag=False, reviewed_by=None) -> ServiceTemplate:
    canon = ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], requires_health_check=flag,
    )
    if reviewed_by is not None:
        ServiceTemplate.objects.filter(pk=canon.pk).update(
            health_check_origin=Origin.CONFIRMED, health_check_confirmed_by=reviewed_by,
            health_check_confirmed_at=REVIEWED_AT, health_check_source_ref="разбор 03.10",
        )
        canon.refresh_from_db()
    return canon


def _model_admin():
    return django_admin.site._registry[ServiceTemplate]


def _form(canon, reviewer, **over) -> ServiceTemplateAdminForm:
    """Форма так, как её собирает админка для того, кто сохраняет."""
    request = RequestFactory().get("/")
    request.user = reviewer
    data = {k: ("" if v is None else v) for k, v in model_to_dict(canon).items()}
    data.update(over)
    for name, value in list(data.items()):
        if isinstance(value, datetime):
            # Админка принимает дату и время двумя полями.
            local = timezone.localtime(value)
            del data[name]
            data[f"{name}_0"], data[f"{name}_1"] = local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")
    return _model_admin().get_form(request, canon, change=True)(data=data, instance=canon)


def _save(curator, form) -> ServiceTemplate:
    request = RequestFactory().post("/")
    request.user = curator
    canon = form.save(commit=False)
    _model_admin().save_model(request, canon, form, change=True)
    return ServiceTemplate.objects.get(pk=canon.pk)


def _codes(form, field) -> list[str]:
    return [e.code for e in form.errors.as_data().get(field, [])]


# ─── очередь ─────────────────────────────────────────────────────────────────


def test_the_admin_uses_the_review_form(curator) -> None:
    request = RequestFactory().get("/")
    request.user = curator

    form = _model_admin().get_form(request)

    assert issubclass(form, ServiceTemplateAdminForm)
    assert form.reviewer == curator


def test_a_form_built_outside_the_admin_does_not_sign_for_nobody(category) -> None:
    canon = _canon(category, "Канон")
    data = {k: ("" if v is None else v) for k, v in model_to_dict(canon).items()}
    data.update(health_check_origin=Origin.CONFIRMED, health_check_source_ref="разбор")

    form = ServiceTemplateAdminForm(data=data, instance=canon)

    assert not form.is_valid()
    assert _codes(form, "health_check_confirmed_by") == ["health_flag_review_requires_who_or_rule"]


def test_the_list_shows_the_flag_and_its_origin(category, curator) -> None:
    _canon(category, "Канон черновой")
    client = Client()
    client.force_login(curator)

    response = client.get(reverse("admin:services_servicetemplate_changelist"))

    assert response.status_code == 200
    columns = _model_admin().get_list_display(response.wsgi_request)
    assert "requires_health_check" in columns and "health_check_origin" in columns


def test_the_queue_of_unreviewed_flags_can_be_filtered(category, curator) -> None:
    _canon(category, "Канон черновой")
    _canon(category, "Канон просмотренный", reviewed_by=curator)
    client = Client()
    client.force_login(curator)
    url = reverse("admin:services_servicetemplate_changelist")

    def names(origin):
        response = client.get(url, {"health_check_origin__exact": origin})
        assert response.status_code == 200
        return sorted(canon.name for canon in response.context["cl"].result_list)

    assert names(Origin.INFERRED) == ["Канон черновой"]
    assert names(Origin.CONFIRMED) == ["Канон просмотренный"]
    # Отбор по параметру Django разрешает и без фильтра в боковой панели;
    # очередь — это когда фильтр куратору ПРЕДЛОЖЕН.
    offered = {spec.field.name for spec in client.get(url).context["cl"].filter_specs if hasattr(spec, "field")}
    assert {"health_check_origin", "requires_health_check"} <= offered


# ─── проверка формы ──────────────────────────────────────────────────────────


def test_a_review_without_a_ground_is_refused_by_the_field(category, curator) -> None:
    canon = _canon(category, "Канон")

    form = _form(canon, curator, health_check_origin=Origin.CONFIRMED, health_check_source_ref="")

    assert not form.is_valid()
    assert _codes(form, "health_check_source_ref") == ["health_flag_review_requires_source_ref"]


def test_a_review_by_both_a_person_and_a_rule_is_refused(category, curator) -> None:
    canon = _canon(category, "Канон")

    form = _form(
        canon, curator, health_check_origin=Origin.CONFIRMED, health_check_source_ref="разбор",
        health_check_confirmed_by=curator.pk, health_check_confirmed_rule="owner_rule",
        health_check_rule_version="1",
    )

    assert not form.is_valid()
    assert _codes(form, "health_check_confirmed_rule") == ["health_flag_review_who_xor_rule"]


def test_a_rule_without_a_version_is_refused(category, curator) -> None:
    canon = _canon(category, "Канон")

    form = _form(
        canon, curator, health_check_origin=Origin.CONFIRMED, health_check_source_ref="разбор",
        health_check_confirmed_rule="owner_rule",
    )

    assert not form.is_valid()
    assert _codes(form, "health_check_rule_version") == ["health_flag_rule_requires_version"]


# ─── подпись ─────────────────────────────────────────────────────────────────


def test_a_review_without_a_named_author_is_signed_by_whoever_saves(category, curator) -> None:
    canon = _canon(category, "Канон")
    form = _form(canon, curator, health_check_origin=Origin.CONFIRMED, health_check_source_ref="разбор владельца 08.10")
    before = timezone.now()
    assert form.is_valid(), form.errors

    saved = _save(curator, form)

    assert saved.health_check_origin == Origin.CONFIRMED
    assert saved.health_check_confirmed_by_id == curator.pk
    assert before <= saved.health_check_confirmed_at <= timezone.now()
    assert saved.health_check_source_ref == "разбор владельца 08.10"


def test_a_named_author_and_date_are_left_as_given(category, curator, another) -> None:
    canon = _canon(category, "Канон")
    form = _form(
        canon, curator, health_check_origin=Origin.CONFIRMED, health_check_source_ref="разбор",
        health_check_confirmed_by=another.pk, health_check_confirmed_at=REVIEWED_AT,
    )
    assert form.is_valid(), form.errors

    saved = _save(curator, form)

    assert (saved.health_check_confirmed_by_id, saved.health_check_confirmed_at) == (another.pk, REVIEWED_AT)


def test_a_review_by_a_rule_is_not_signed_by_a_person(category, curator) -> None:
    canon = _canon(category, "Канон")
    form = _form(
        canon, curator, health_check_origin=Origin.CONFIRMED, health_check_source_ref="решение владельца",
        health_check_confirmed_rule="owner_rule", health_check_rule_version="1",
    )
    assert form.is_valid(), form.errors

    saved = _save(curator, form)

    assert (saved.health_check_confirmed_by_id, saved.health_check_confirmed_rule) == (None, "owner_rule")
    assert saved.health_check_confirmed_at is not None


def test_a_draft_flag_is_saved_unsigned(category, curator) -> None:
    canon = _canon(category, "Канон")
    form = _form(canon, curator, name_short="Канон, короче")
    assert form.is_valid(), form.errors

    saved = _save(curator, form)

    assert saved.health_check_origin == Origin.INFERRED
    assert (saved.health_check_confirmed_by_id, saved.health_check_confirmed_at) == (None, None)


def test_saving_a_reviewed_canon_again_keeps_the_first_signature(category, curator, another) -> None:
    canon = _canon(category, "Канон", reviewed_by=another)
    form = _form(canon, curator, name_short="Канон, короче")
    assert form.is_valid(), form.errors

    saved = _save(curator, form)

    assert (saved.health_check_confirmed_by_id, saved.health_check_confirmed_at) == (another.pk, REVIEWED_AT)


# ─── флаг под прежним просмотром ─────────────────────────────────────────────


def test_a_flag_flipped_under_the_old_review_is_not_let_through_silently(category, curator) -> None:
    canon = _canon(category, "Канон", flag=True, reviewed_by=curator)

    form = _form(canon, curator, requires_health_check=False)

    assert not form.is_valid()
    assert _codes(form, "requires_health_check") == ["health_flag_changed_needs_new_review"]
    assert ServiceTemplate.objects.get(pk=canon.pk).requires_health_check is True


def test_a_flag_flipped_with_a_new_review_is_signed_anew(category, curator, another) -> None:
    canon = _canon(category, "Канон", flag=True, reviewed_by=another)
    form = _form(
        canon, curator, requires_health_check=False, health_check_confirmed_by="", health_check_confirmed_at="",
        health_check_source_ref="повторный разбор 08.10",
    )
    assert form.is_valid(), form.errors

    saved = _save(curator, form)

    assert (saved.requires_health_check, saved.health_check_origin) == (False, Origin.CONFIRMED)
    assert saved.health_check_confirmed_by_id == curator.pk
    assert saved.health_check_confirmed_at > REVIEWED_AT
    assert saved.health_check_source_ref == "повторный разбор 08.10"


def test_a_flag_sent_back_to_draft_may_be_flipped(category, curator) -> None:
    canon = _canon(category, "Канон", flag=True, reviewed_by=curator)
    form = _form(
        canon, curator, requires_health_check=False, health_check_origin=Origin.INFERRED,
        health_check_confirmed_by="", health_check_confirmed_at="", health_check_source_ref="",
    )
    assert form.is_valid(), form.errors

    saved = _save(curator, form)

    assert (saved.requires_health_check, saved.health_check_origin) == (False, Origin.INFERRED)


# ─── массового подтверждения нет ─────────────────────────────────────────────


def test_there_is_no_bulk_review_action(curator) -> None:
    request = RequestFactory().get("/")
    request.user = curator

    actions = set(_model_admin().get_actions(request))

    assert "delete_selected" in actions or actions == set()  # положительная пара: действия читаются
    assert not {name for name in actions if "health" in name or "confirm" in name or "review" in name}
    assert "requires_health_check" not in _model_admin().list_editable
    assert "health_check_origin" not in _model_admin().list_editable
