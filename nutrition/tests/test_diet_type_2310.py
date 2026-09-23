"""Тип питания — закрытый список, а не свободная строка (DRF-2310, каталог).

Решение владельца 23.09.2026 (§77 п. 5–7). Состав списка назвал владелец:
«без ограничений · вегетарианство · веганство · кето · халяль · кошер ·
другое словами». Состав совпал со словарём команд забывания бота
(`apps/persona/memory_commands.py`), но ОСНОВАНИЕ здесь — слово владельца, а
не словарь: матчер фраз продуктовым решением не является и им не станет.

До этого листа `diet_preference` принимал любую строку до 32 символов, а у
колонки стояло умолчание `"none"` — то есть каждая существующая строка уже
«что-то отвечала», хотя никого не спрашивали.

Два решения владельца, которые держат эти узлы:
* **«Пропустить» — отдельный ответ**, не равный «без ограничений»: первое —
  отсутствие ответа, второе — ответ;
* **Легаси хранится молча, не стирается, но шаг считается НЕПРОЙДЕННЫМ.**
  Кампании переспроса нет; Ayla спросит, когда речь зайдёт сама.

* d1 — значения списка принимаются, чужое отвергается;
* d2 — «другое» несёт слова, и без слов оно не ответ;
* d3 — прежнее значение хранится, но ответом не считается;
* d4 — названный ответ снимает пометку легаси (подтверждение);
* d5 — пропуск — не «без ограничений» и ответом не считается;
* d6 — умолчание колонки (`none`) ответом не считается никогда;
* d7 — команда пометки DRF-2279 типа питания НЕ касается: пометить по
  значению значило бы переспросить каждого, кто уже ответил;
* d8 — эхо прежнего значения (`none`) принимается и ничего не меняет:
  ручка его сама и отдаёт, и вызывающий возвращает тело целиком;
* d9 — ПУТЬ РУЧКИ: ответ со словами доезжает и возвращается признаком
  `diet_answered`; пропуск приходит через `_skipped_fields` и столбца не
  трогает; ответ после пропуска снимает флаг; пропуск поверх ответа ответа
  не отменяет; слова без ответа отвергаются; пропуск не роняет предложение.
"""
from __future__ import annotations

import pytest

from nutrition.models import NutritionProfile
from django.utils import timezone

from nutrition.services.diet_type import (
    DIET_FIELD,
    DIET_SKIPPED_FLAG,
    answer_diet,
    diet_answered,
    skip_diet,
)

pytestmark = pytest.mark.django_db


def _profile(**kw) -> NutritionProfile:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username=f"diet{kw.pop('n', 1)}", password="x", phone=f"+7999111{kw.pop('p', '0001')}"
    )
    return NutritionProfile.objects.create(user=user, **kw)


class TestD1TheVocabularyIsClosed:
    @pytest.mark.parametrize("value", [v for v, _ in NutritionProfile.DietType.choices])
    def test_every_named_value_is_an_answer(self, value: str) -> None:
        note = "по назначению врача" if value == NutritionProfile.DietType.OTHER else ""
        p = _profile(n=value, p=value[:4])
        answer_diet(p, value, note=note)

        assert diet_answered(p) is True

    def test_a_value_outside_the_list_is_not_an_answer(self) -> None:
        p = _profile(n=2, p="0002", diet_preference="палео", diet_answered_at=timezone.now())

        assert diet_answered(p) is False


class TestD2OtherCarriesWords:
    @pytest.mark.parametrize("note", ["", "   "])
    def test_other_without_words_is_not_an_answer(self, note: str) -> None:
        """«Другое словами» без слов — не ответ: сказано ничего. Пробелы —
        тоже ничего, иначе пробел сошёл бы за ответ."""
        p = _profile(
            n=3,
            p="0003",
            diet_preference=NutritionProfile.DietType.OTHER,
            diet_note=note,
            diet_answered_at=timezone.now(),
        )

        assert diet_answered(p) is False

    def test_other_with_words_is_an_answer(self) -> None:
        p = _profile(n=4, p="0004")
        answer_diet(p, NutritionProfile.DietType.OTHER, note="без лактозы")

        assert diet_answered(p) is True


class TestD3LegacyIsKeptButIsNotAnAnswer:
    def test_a_previous_value_is_stored_and_not_counted(self) -> None:
        """Хранится молча, не стирается — но шаг не пройден.

        Значение здесь — слово ИЗ списка: прежнее «vegan» писали свободной
        строкой, и от ответа его нельзя отличить по самому значению. Отличает
        факт ответа: его не было.
        """
        p = _profile(n=5, p="0005", diet_preference=NutritionProfile.DietType.VEGAN)

        assert p.diet_preference == NutritionProfile.DietType.VEGAN  # наличие: не стёрто
        assert p.diet_answered_at is None
        assert diet_answered(p) is False


class TestD4AnsweringOverAPreviousValue:
    @pytest.mark.parametrize(
        "named", [NutritionProfile.DietType.KETO, NutritionProfile.DietType.HALAL]
    )
    def test_the_same_and_a_different_value_both_count(self, named: str) -> None:
        """Подтверждение прежнего значения — такой же ответ, как смена."""
        p = _profile(n=6, p="0006", diet_preference=NutritionProfile.DietType.KETO)
        assert diet_answered(p) is False  # наличие: до ответа шаг не пройден

        answer_diet(p, named)

        assert diet_answered(p) is True
        assert p.diet_preference == named
        assert p.legacy_default_inputs == []  # чужой механизм не тронут


class TestD4bSwitchingAwayFromOtherClearsTheWords:
    def test_the_words_belong_to_other_and_go_with_it(self) -> None:
        """Слова принадлежат ответу «другое». Сменил ответ — слова уходят:
        оставить их рядом с «кето» значило бы приписать человеку подробность
        к ответу, которого он про неё не давал. Это решение, а не побочный
        эффект тернарника, поэтому оно закреплено."""
        p = _profile(n=17, p="0017")
        answer_diet(p, NutritionProfile.DietType.OTHER, note="без лактозы")
        assert p.diet_note == "без лактозы"  # наличие: слова записаны

        answer_diet(p, NutritionProfile.DietType.KETO)

        assert p.diet_note == ""


class TestD5SkipIsItsOwnAnswer:
    def test_skipped_is_not_unrestricted_and_not_an_answer(self) -> None:
        """Решение владельца: пропуск ≠ «без ограничений». Первое — отсутствие
        ответа, второе — ответ."""
        p = _profile(n=7, p="0007")

        skip_diet(p)

        assert p.diet_preference != NutritionProfile.DietType.UNRESTRICTED
        assert diet_answered(p) is False
        assert p.health_flags.get(f"{DIET_FIELD}_skipped") is True  # пропуск записан


class TestD6TheColumnDefaultIsNotAnAnswer:
    def test_none_never_counts(self) -> None:
        """У колонки умолчание `none`: считай его ответом — и каждая строка,
        заведённая до вопроса, молча оказалась бы «без ограничений»."""
        p = _profile(n=8, p="0008")

        assert p.diet_preference == "none"  # наличие: умолчание на месте
        assert diet_answered(p) is False


class TestD7TheMarkingCommandIsNotUsedHere:
    def test_the_legacy_marking_command_leaves_diet_alone(self) -> None:
        """Пометка DRF-2279 узнаёт прежнее значение ПО САМОМУ значению — здесь
        это невозможно: «vegan» одинаково бывает и прежней строкой, и ответом.
        Пометь команда такие строки — она переспросила бы каждого, кто уже
        ответил, то есть устроила бы ровно ту кампанию, которой владелец
        сказал не быть. Узел держит, что команда типа питания не касается."""
        from io import StringIO

        from django.core.management import call_command

        answered = _profile(n=9, p="0009")
        answer_diet(answered, NutritionProfile.DietType.VEGAN)
        answered.save(update_fields=["diet_preference", "diet_note", "diet_answered_at"])

        call_command("mark_legacy_default_inputs", "--apply", stdout=StringIO())
        answered.refresh_from_db()

        assert diet_answered(answered) is True  # ответ остался ответом
        assert DIET_FIELD not in (answered.legacy_default_inputs or [])


class TestD8TheEchoOfTheOldValueIsAccepted:
    def test_posting_back_what_the_endpoint_returned_changes_nothing(self) -> None:
        """Ручка отдаёт `"diet_preference": "none"`. Вызывающий, вернувший
        тело обратно (обычный read-modify-write), не должен получать отказ на
        поле, которого он не трогал, — и ответом это эхо тоже не становится."""
        from nutrition.services.profile_upsert_service import upsert_profile

        p = _profile(n=11, p="0011")

        upsert_profile(
            user=p.user,
            external_user_id="bot:max:2310011",
            idempotency_key=None,
            payload={"diet_preference": "none"},
        )
        p.refresh_from_db()

        assert p.diet_preference == "none"  # наличие: значение на месте
        assert diet_answered(p) is False
        assert DIET_FIELD not in (p.legacy_default_inputs or [])

    def test_a_value_outside_the_list_is_refused_by_the_serializer(self) -> None:
        from nutrition.serializers import NutritionProfileUpsertSerializer as S

        ser = S(data={"diet_preference": "палео"})

        assert ser.is_valid() is False
        assert "diet_preference" in ser.errors


class TestD9TheEndpointPath:
    """Разбор типа питания живёт в upsert, и проверять его надо оттуда:
    узлы на самой службе не доказывают, что ручка её вызывает."""

    def _post(self, user, payload: dict) -> dict:
        from nutrition.services.profile_upsert_service import upsert_profile

        return upsert_profile(
            user=user,
            external_user_id=f"bot:max:2310{user.pk}",
            idempotency_key=None,
            payload=payload,
        )

    def test_an_answer_with_words_arrives_and_comes_back(self) -> None:
        p = _profile(n=12, p="0012")

        body = self._post(p.user, {
            "diet_preference": NutritionProfile.DietType.OTHER,
            "diet_note": "без лактозы",
        })
        p.refresh_from_db()

        assert body["diet_answered"] is True
        assert body["diet_note"] == "без лактозы"
        assert body["diet_preference"] == NutritionProfile.DietType.OTHER
        assert diet_answered(p) is True

    def test_a_skip_arrives_and_leaves_the_column_alone(self) -> None:
        p = _profile(n=13, p="0013")

        body = self._post(p.user, {"_skipped_fields": [DIET_FIELD]})
        p.refresh_from_db()

        assert body["diet_answered"] is False
        assert p.diet_preference == "none"  # молчание не стало ответом
        assert p.health_flags.get(DIET_SKIPPED_FLAG) is True

    def test_an_answer_after_a_skip_clears_the_skip(self) -> None:
        p = _profile(n=14, p="0014")
        self._post(p.user, {"_skipped_fields": [DIET_FIELD]})

        body = self._post(p.user, {"diet_preference": NutritionProfile.DietType.VEGAN})
        p.refresh_from_db()

        assert body["diet_answered"] is True  # наличие: ответ принят
        assert DIET_SKIPPED_FLAG not in (p.health_flags or {})

    def test_a_skip_after_an_answer_does_not_retract_it(self) -> None:
        """Два противоположных факта об одном вопросе — «назвал» и «не
        назвал» — рядом не лежат. Ответ старше и сильнее."""
        p = _profile(n=15, p="0015")
        self._post(p.user, {"diet_preference": NutritionProfile.DietType.KETO})

        body = self._post(p.user, {"_skipped_fields": [DIET_FIELD]})
        p.refresh_from_db()

        assert body["diet_answered"] is True
        assert DIET_SKIPPED_FLAG not in (p.health_flags or {})

    def test_words_without_an_answer_are_refused(self) -> None:
        from nutrition.serializers import NutritionProfileUpsertSerializer

        ser = NutritionProfileUpsertSerializer(data={"diet_note": "без глютена"})

        assert ser.is_valid() is False
        assert "diet_note" in ser.errors

    def test_the_skip_does_not_drop_a_pending_proposal(self) -> None:
        """Тип питания в расчёт не входит: `targets_recompute_gate` называет
        `{"diet_preference": "vegetarian"}` телом, которое ориентиров не
        трогает. Пропуск того же вопроса — тем более."""
        p = _profile(n=16, p="0016")
        NutritionProfile.objects.filter(pk=p.pk).update(
            pending_proposal={"daily_kcal": 1800},
        )

        self._post(p.user, {"_skipped_fields": [DIET_FIELD]})
        p.refresh_from_db()

        assert p.pending_proposal == {"daily_kcal": 1800}
