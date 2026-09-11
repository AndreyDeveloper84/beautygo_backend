"""Граница каталога не принимает параметры тела без основания (§92, N-a2).

Замер, ради которого этот сторож заведён — пилот, 10.09.2026:

    профилей 6 · weight_kg заполнен у 4 (65.0, 67.0, 90.0, 95.0)
    disclaimer_acked не пуст: 0
    ConsentRecord в боте: granted personal_data — 1 из 6
    видов `personal_calculation` и `nutrition_diary` НЕ СУЩЕСТВУЕТ

Все шесть профилей собраны до того, как согласие требуемого вида начало
существовать. Экспозиция — не ноль и не «риск на будущее»: данные уже
лежат. Сторож закрывает приём НОВЫХ; что делать с уже собранным —
решение владельца, оно в реестре, и это названный предел среза.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.personal_calculation_consent import (
    PERSONAL_CALCULATION,
    PERSONAL_CALCULATION_FIELDS,
    PersonalCalculationConsentRequired,
    gated_fields_in,
    require_consent,
)
from users.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/internal/profile/"
EXTERNAL_USER_ID = "bot:na2:1"
SERVICE_TOKEN = "na2-service-token"  # pragma: allowlist secret

VALID_CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}


@pytest.fixture(autouse=True)
def _token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def person(db):
    return User.objects.create(
        username=EXTERNAL_USER_ID, role="client", is_proxy=True,
    )


def _api() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_SERVICE_TOKEN"] = SERVICE_TOKEN
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _post(body: dict):
    return _api().post(URL, body, format="json")


class TestTheBodyIsRefusedWithoutABasis:
    def test_weight_without_an_attestation_is_refused(self, person):
        """Ровно та поверхность, которую §92 назвал горящей."""
        r = _post({"weight_kg": 70.0})

        assert r.status_code == 422, r.content
        error = r.json()["error"]
        assert error["code"] == "CONSENT_REQUIRED"
        assert error["code"] != "VALIDATION_ERROR"
        assert error["details"]["consent_type"] == PERSONAL_CALCULATION
        assert error["details"]["fields"] == ["weight_kg"]

    def test_the_refusal_names_every_gated_field_it_saw(self, person):
        """Вызывающий чинит своё утверждение, а не угадывает поле."""
        r = _post({"weight_kg": 70.0, "height_cm": 170, "diet_preference": "any"})

        fields = r.json()["error"]["details"]["fields"]
        assert fields == ["height_cm", "weight_kg"]
        # Незакрытое поле в список отказа не попадает — иначе вызывающий
        # начнёт искать согласие там, где оно не нужно.
        assert "diet_preference" not in fields

    @pytest.mark.parametrize("field,value", [
        ("gender", "female"),
        ("age", 30),
        ("height_cm", 170),
        ("weight_kg", 70.0),
        ("weight_range", "70-80"),
        ("activity_coefficient", 1.4),
        ("goal", "maintain"),
    ])
    def test_every_gated_field_is_refused_on_its_own(self, person, field, value):
        """Каждое поле §92 закрыто ПООДИНОЧКЕ.

        Проверка списком, а не одним примером: гейт, закрывающий вес и
        пропускающий рост, исполнил бы букву правила и оставил бы пять
        параметров из шести собранными без основания.
        """
        r = _post({field: value})

        assert r.status_code == 422, (field, r.content)
        assert r.json()["error"]["details"]["fields"] == [field]

    def test_the_field_list_matches_the_owner_decision(self):
        """Состав закрытого набора — против §92, а не против кода.

        §92 перечисляет дословно: вес, рост, возраст, физиологический
        пол, активность и цель. Седьмым добавлен `weight_range` — тот же
        вес меньшего разрешения; решение исполнителя, названное в
        докстринге модуля.
        """
        assert PERSONAL_CALCULATION_FIELDS == {
            "gender", "age", "height_cm", "weight_kg",
            "weight_range", "activity_coefficient", "goal",
        }


class TestAnAttestationOpensTheDoor:
    def test_weight_with_an_attestation_is_accepted(self, person):
        """Положительная стража: сторож закрывает НЕ всё.

        Без неё все тесты отказа зеленели бы и на коде, который отвергает
        любой запрос, — то есть на сломанной ручке вместо работающего
        сторожа.
        """
        r = _post({"weight_kg": 70.0, "consent": VALID_CONSENT})

        assert r.status_code == 200, r.content
        assert r.json()["data"]["weight_kg"] == 70.0

    def test_the_diary_is_not_closed_by_the_gate(self, person):
        """§92 дословно: «отказ не закрывает дневник».

        Запрос без параметров тела согласия этого вида не требует, и
        требовать его значило бы закрыть базовую функцию ради расчёта.
        """
        r = _post({"diet_preference": "vegetarian", "timezone": "Europe/Moscow"})

        assert r.status_code == 200, r.content


class TestAnAttestationThatSaysNothingIsNotAnAttestation:
    def test_a_wrong_consent_type_is_refused(self, person):
        """Согласие на дневник не открывает расчёт — §92 развёл их нарочно."""
        r = _post({
            "weight_kg": 70.0,
            "consent": {"type": "nutrition_diary", "document_version": "v1"},
        })

        assert r.status_code == 422, r.content
        assert r.json()["error"]["code"] == "CONSENT_REQUIRED"

    def test_a_missing_version_is_refused(self, person):
        """Версия обязательна: §92 требует хранить версию текста.

        Согласие без версии через полгода нельзя отличить от согласия на
        другой текст — принять его значило бы записать утверждение,
        которое ничего не утверждает.
        """
        r = _post({"weight_kg": 70.0, "consent": {"type": PERSONAL_CALCULATION}})

        assert r.status_code == 400, r.content
        # Здесь отказ ПО ФОРМАТУ, и это правильно: поля нет в теле, до
        # сторожа согласия запрос не доходит. Проверяется, что дыры нет,
        # а не то, каким именем её закрыли.
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_a_blank_version_does_not_open_the_door(self, person):
        """Версия из пробелов дверь не открывает — закрывает сериализатор.

        Я ожидал, что до сторожа дойдёт и откажет он; на деле `CharField`
        отвергает пустую строку раньше, и запрос получает 400
        `VALIDATION_ERROR`. Записан фактический заслон, а не тот, который
        я собирался проверить: важно, что дыры нет, а не чьим именем она
        закрыта. Собственный отказ сторожа проверяется юнитом ниже — он
        вызывается не только из этой ручки.
        """
        r = _post({
            "weight_kg": 70.0,
            "consent": {"type": PERSONAL_CALCULATION, "document_version": "   "},
        })

        assert r.status_code == 400, r.content
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert NutritionProfile.objects.filter(
            user__username=EXTERNAL_USER_ID
        ).exclude(weight_kg=None).count() == 0


class TestTheGuardItself:
    """Юниты на сам сторож — он вызывается не только из этой ручки.

    Через HTTP часть его веток недостижима: сериализатор отвергает
    пустую версию раньше, а ноль в весе не проходит диапазон. Ветки от
    этого не перестают быть нужными — они защищают следующего
    вызывающего, у которого сериализатора может не быть.
    """

    def test_presence_not_truthiness(self):
        """`weight_kg: 0` — это тоже утверждение о теле.

        Ручка PATCH-семантична: проверка «если значение» пропустила бы
        ноль молча, а ноль здесь означает не «поля нет», а «вес нулевой».
        """
        assert gated_fields_in({"weight_kg": 0}) == {"weight_kg"}
        assert gated_fields_in({"gender": ""}) == {"gender"}

    def test_an_ungated_payload_needs_no_attestation(self):
        """Положительная стража к предыдущему."""
        assert gated_fields_in({"diet_preference": "any", "timezone": "UTC"}) == set()
        require_consent({"diet_preference": "any"})  # не бросает

    def test_a_blank_version_is_refused_by_the_guard(self):
        with pytest.raises(PersonalCalculationConsentRequired) as exc:
            require_consent({
                "weight_kg": 70.0,
                "consent": {
                    "type": PERSONAL_CALCULATION, "document_version": "   ",
                },
            })
        assert exc.value.fields == ["weight_kg"]

    def test_an_attestation_that_is_not_a_mapping_is_refused(self):
        """`consent: true` — булев признак вместо утверждения.

        Ровно та форма, которую §92 запрещает: без версии согласие через
        полгода не отличить от согласия на другой текст.
        """
        with pytest.raises(PersonalCalculationConsentRequired):
            require_consent({"weight_kg": 70.0, "consent": True})
