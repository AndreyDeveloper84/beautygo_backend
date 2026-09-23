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
* d7 — команда пометки: прежнее значение, случайно совпавшее со словом из
  списка, метится и ответом не становится; сухой прогон ничего не пишет;
* d8 — эхо прежнего значения (`none`) принимается и ничего не меняет:
  ручка его сама и отдаёт, и вызывающий возвращает тело целиком.
"""
from __future__ import annotations

import pytest

from nutrition.models import NutritionProfile
from nutrition.services.diet_type import DIET_FIELD, diet_answered

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
        p = _profile(n=value, p=value[:4], diet_preference=value, diet_note=note)

        assert diet_answered(p) is True

    def test_a_value_outside_the_list_is_not_an_answer(self) -> None:
        p = _profile(n=2, p="0002", diet_preference="палео")

        assert diet_answered(p) is False


class TestD2OtherCarriesWords:
    def test_other_without_words_is_not_an_answer(self) -> None:
        """«Другое словами» без слов — не ответ: сказано ничего."""
        p = _profile(n=3, p="0003", diet_preference=NutritionProfile.DietType.OTHER)

        assert diet_answered(p) is False

    def test_other_with_words_is_an_answer(self) -> None:
        p = _profile(
            n=4,
            p="0004",
            diet_preference=NutritionProfile.DietType.OTHER,
            diet_note="без лактозы",
        )

        assert diet_answered(p) is True


class TestD3LegacyIsKeptButIsNotAnAnswer:
    def test_a_marked_value_is_stored_and_not_counted(self) -> None:
        """Хранится молча, не стирается — но шаг не пройден."""
        p = _profile(
            n=5,
            p="0005",
            diet_preference=NutritionProfile.DietType.VEGAN,
            legacy_default_inputs=[DIET_FIELD],
        )

        assert p.diet_preference == NutritionProfile.DietType.VEGAN  # наличие: не стёрто
        assert diet_answered(p) is False


class TestD4ANamedAnswerClearsTheMark:
    def test_the_same_value_named_again_counts(self) -> None:
        """Подтверждение — тот же механизм, что у темпа и активности."""
        from nutrition.services.diet_type import answer_diet

        p = _profile(
            n=6,
            p="0006",
            diet_preference=NutritionProfile.DietType.KETO,
            legacy_default_inputs=[DIET_FIELD, "pace"],
        )

        answer_diet(p, NutritionProfile.DietType.KETO)

        assert diet_answered(p) is True
        assert p.legacy_default_inputs == ["pace"]  # чужая пометка не тронута


class TestD5SkipIsItsOwnAnswer:
    def test_skipped_is_not_unrestricted_and_not_an_answer(self) -> None:
        """Решение владельца: пропуск ≠ «без ограничений». Первое — отсутствие
        ответа, второе — ответ."""
        from nutrition.services.diet_type import skip_diet

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


class TestD7TheMarkingCommandCoversDiet:
    def test_a_value_that_coincides_with_the_list_is_marked_not_counted(self) -> None:
        """Прежнее «vegan» писали свободной строкой, и от ответа оно неотличимо.
        Без пометки такая строка молча сошла бы за ответ, которого никто не
        давал, — а решение владельца прямо говорит: шаг не пройден."""
        from io import StringIO

        from django.core.management import call_command

        p = _profile(n=9, p="0009", diet_preference=NutritionProfile.DietType.VEGAN)

        out = StringIO()
        call_command("mark_legacy_default_inputs", stdout=out)
        p.refresh_from_db()
        assert p.legacy_default_inputs == []  # сухой прогон ничего не пишет
        assert diet_answered(p) is True  # до пометки строка неотличима от ответа

        call_command("mark_legacy_default_inputs", "--apply", stdout=StringIO())
        p.refresh_from_db()

        assert p.diet_preference == NutritionProfile.DietType.VEGAN  # не стёрто
        assert DIET_FIELD in p.legacy_default_inputs
        assert diet_answered(p) is False

    def test_the_column_default_is_not_marked(self) -> None:
        """`none` метить нечем и незачем: он и так не ответ, а пометка на нём
        означала бы, что когда-то был ответ."""
        from io import StringIO

        from django.core.management import call_command

        p = _profile(n=10, p="0010")

        call_command("mark_legacy_default_inputs", "--apply", stdout=StringIO())
        p.refresh_from_db()

        assert diet_answered(p) is False  # наличие: шаг по-прежнему не пройден
        assert DIET_FIELD not in p.legacy_default_inputs


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
