"""DRF-2886 — внутренний контракт личного контекста умеет очистить одно поле.

До правки цену через ``PATCH`` нельзя было очистить вовсе: ``null`` → 400,
пустая строка падала на Decimal-колонке при сохранении. После «забудь про
бюджет» значение оставалось в анкете и продолжало уходить модели.

Кодировка: ``value: null`` — «очистить поле»; оно возвращается к умолчанию
модели той же таблицей, что у поштучного сброса клиентского приложения.

Узлы держат:

* ``null`` очищает поле каждого вида — число, список, строку, флаг;
* очищенная цена не возвращается ни в ответе, ни в чтении, ни в подсказке
  модели;
* у очищенного поля записан источник очистки;
* очистка и запись идут одной пачкой;
* цена — число, не отрицательное; пустая строка и мусор — отказ с именем
  поля, а не падение, и пачка с отказом не пишет ничего;
* прежние записи значений работают как раньше;
* таблица умолчаний — та же, что у клиентского приложения, и покрывает
  каждое поле зелёной зоны.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from ai.personal_context_hint import format_personal_context_hint
from users.internal_personal_context_api import _FIELD_DEFAULTS
from users.models import User, UserPersonalContext
from users.personal_context_views import _GREEN_ZONE_FIELDS, UserPersonalContextFieldDeleteView

from .conftest import name_subject

pytestmark = pytest.mark.django_db

TOKEN = "test-internal-token"  # noqa: S105 — test constant, not a real secret

FILLED = {
    "price_range_min": Decimal("1500.00"),
    "price_range_max": Decimal("4000.00"),
    "min_rating_preference": 4.5,
    "preferred_time_slots": ["evening"],
    "favorite_masters": ["Анна"],
    "diet_type": "vegetarian",
    "home_district": "Центр",
    "prefers_flexible_cancellation": True,
}


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def user() -> User:
    return User.objects.create_user(
        username="clear_2886_owner", password="x", role="client", phone="+79995552886",
    )


@pytest.fixture
def ctx(user) -> UserPersonalContext:
    return UserPersonalContext.objects.create(
        user=user, data_sources=dict.fromkeys(FILLED, "conversational"), **FILLED,
    )


def _api(user) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = name_subject(user)
    return client


def _url(user) -> str:
    return f"/api/v1/internal/users/{user.id}/personal-context/"


def _patch(user, *updates):
    return _api(user).patch(_url(user), {"updates": list(updates)}, format="json")


def _clear(field, source="explicit"):
    return {"field": field, "value": None, "source": source}


# ─── очистка ─────────────────────────────────────────────────────────────────


def test_forget_the_budget_clears_both_bounds(user, ctx) -> None:
    response = _patch(user, _clear("price_range_min"), _clear("price_range_max"))

    assert response.status_code == 200, response.data
    context = response.data["data"]["context"]
    assert (context["price_range_min"], context["price_range_max"]) == (None, None)
    ctx.refresh_from_db()
    assert (ctx.price_range_min, ctx.price_range_max) == (None, None)
    # Положительная пара: остальное не тронуто.
    assert ctx.home_district == "Центр" and ctx.preferred_time_slots == ["evening"]


def test_a_cleared_budget_no_longer_reaches_the_model(user, ctx) -> None:
    assert "бюджет" in format_personal_context_hint(ctx)  # положительная пара

    _patch(user, _clear("price_range_min"), _clear("price_range_max"))

    ctx.refresh_from_db()
    assert "бюджет" not in format_personal_context_hint(ctx)
    read = _api(user).get(_url(user)).data["data"]["context"]
    assert (read["price_range_min"], read["price_range_max"]) == (None, None)


@pytest.mark.parametrize(
    ("field", "default"),
    [
        ("price_range_max", None),
        ("min_rating_preference", None),
        ("preferred_time_slots", []),
        ("favorite_masters", []),
        ("diet_type", ""),
        ("home_district", ""),
        ("prefers_flexible_cancellation", False),
    ],
)
def test_null_resets_a_field_of_every_kind_to_its_default(user, ctx, field, default) -> None:
    assert getattr(ctx, field) != default  # положительная пара: было заполнено

    response = _patch(user, _clear(field))

    assert response.status_code == 200, response.data
    ctx.refresh_from_db()
    assert getattr(ctx, field) == default
    assert response.data["data"]["context"][field] == default


def test_the_cleared_field_carries_the_source_of_the_clearing(user, ctx) -> None:
    response = _patch(user, _clear("price_range_max", source="explicit"))

    ctx.refresh_from_db()
    assert ctx.data_sources["price_range_max"] == "explicit"
    assert response.data["data"]["data_sources"]["price_range_max"] == "explicit"
    assert ctx.data_sources["price_range_min"] == "conversational"


def test_clearing_and_writing_go_in_one_batch(user, ctx) -> None:
    response = _patch(
        user,
        _clear("price_range_min"),
        {"field": "price_range_max", "value": "6000", "source": "explicit"},
        {"field": "home_district", "value": "Север", "source": "explicit"},
    )

    assert response.status_code == 200, response.data
    ctx.refresh_from_db()
    assert (ctx.price_range_min, ctx.price_range_max, ctx.home_district) == (
        None, Decimal("6000"), "Север",
    )


def test_clearing_an_empty_profile_is_harmless(user) -> None:
    response = _patch(user, _clear("price_range_max"))

    assert response.status_code == 200, response.data
    assert UserPersonalContext.objects.get(user=user).price_range_max is None


# ─── цена — число ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["3500", "3500.50", 3500, 3500.5, "0"])
def test_a_price_is_written_as_before(user, ctx, value) -> None:
    response = _patch(user, {"field": "price_range_max", "value": value, "source": "explicit"})

    assert response.status_code == 200, response.data
    ctx.refresh_from_db()
    assert ctx.price_range_max == Decimal(str(value))


@pytest.mark.parametrize("value", ["", "   ", "много", "-100", -1, True, [3000], {"max": 3000}, "NaN"])
def test_a_price_that_is_not_a_number_is_refused_not_crashed(user, ctx, value) -> None:
    response = _patch(user, {"field": "price_range_max", "value": value, "source": "explicit"})

    assert response.status_code == 400, response.data
    ctx.refresh_from_db()
    assert ctx.price_range_max == Decimal("4000.00")


def test_a_refused_batch_writes_nothing(user, ctx) -> None:
    response = _patch(
        user,
        {"field": "home_district", "value": "Север", "source": "explicit"},
        _clear("price_range_min"),
        {"field": "price_range_max", "value": "", "source": "explicit"},
    )

    assert response.status_code == 400, response.data
    ctx.refresh_from_db()
    assert (ctx.home_district, ctx.price_range_min, ctx.price_range_max) == (
        "Центр", Decimal("1500.00"), Decimal("4000.00"),
    )
    assert ctx.data_sources["home_district"] == "conversational"


def test_the_refusal_names_the_field_and_the_way_to_clear(user, ctx) -> None:
    response = _patch(user, {"field": "price_range_max", "value": "", "source": "explicit"})

    assert "price_range_max" in str(response.data)
    assert "null" in str(response.data)


# ─── прежнее поведение ───────────────────────────────────────────────────────


def test_other_fields_are_written_as_before(user, ctx) -> None:
    response = _patch(
        user,
        {"field": "preferred_time_slots", "value": ["morning"], "source": "behavioral"},
        {"field": "prefers_flexible_cancellation", "value": False, "source": "explicit"},
    )

    assert response.status_code == 200, response.data
    ctx.refresh_from_db()
    assert ctx.preferred_time_slots == ["morning"]
    assert ctx.prefers_flexible_cancellation is False
    assert ctx.data_sources["preferred_time_slots"] == "behavioral"


def test_a_missing_value_is_still_refused(user, ctx) -> None:
    """``null`` — «очистить»; отсутствие ключа ``value`` — по-прежнему ошибка."""
    response = _api(user).patch(
        _url(user), {"updates": [{"field": "price_range_max", "source": "explicit"}]}, format="json",
    )

    assert response.status_code == 400
    ctx.refresh_from_db()
    assert ctx.price_range_max == Decimal("4000.00")


# ─── таблица умолчаний ───────────────────────────────────────────────────────


def test_the_defaults_are_the_client_apps_and_cover_the_green_zone() -> None:
    assert _FIELD_DEFAULTS is UserPersonalContextFieldDeleteView._FIELD_DEFAULTS
    assert set(_FIELD_DEFAULTS) == set(_GREEN_ZONE_FIELDS)
    for field, make in _FIELD_DEFAULTS.items():
        column = UserPersonalContext._meta.get_field(field)
        default = make()
        assert default is None or default == column.get_default(), field
        if default is None:
            assert column.null, field
