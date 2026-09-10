"""Удаление параметров тела: что стирается, что нет, и когда именно (§120).

Команда, а не миграция — намеренно: слияние в `dev` есть выкладка, и
миграция стёрла бы данные живых людей в момент слияния, то есть как
побочный эффект работы очереди PR. Здесь удаление отделено от слияния во
времени, и тесты закрепляют именно это: **без `--apply` не меняется
ничего**.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from nutrition.models import NutritionProfile
from users.models import User

pytestmark = pytest.mark.django_db


def _profile(username, **over):
    user = User.objects.create(username=username, role="client", is_proxy=True)
    fields = dict(
        user=user,
        gender="female",
        age=40,
        height_cm=165,
        weight_kg=70.0,
        activity_coefficient=1.4,
        goal="maintain",
        bmr=1392,
        daily_kcal=1590,
        # Состояние ПОСЛЕ N-b: ориентиры очищены, происхождения нет.
        # Без этого сторож порядка (см. класс ниже) отказал бы каждому
        # тесту удаления — и отказал бы правильно.
        targets_source=NutritionProfile.TargetsSource.NONE,
    )
    fields.update(over)
    return NutritionProfile.objects.create(**fields)


def _run(*args):
    out = StringIO()
    call_command("purge_unconsented_body_parameters", *args, stdout=out)
    return out.getvalue()


class TestWithoutApplyNothingChanges:
    def test_a_dry_run_leaves_every_value_in_place(self):
        """Главное свойство команды: запуск без флага безопасен.

        Необратимое действие требует отдельного слова, а не отдельного
        везения. Тест закрепляет, что слово обязательно.
        """
        profile = _profile("purge-dry")

        _run()

        profile.refresh_from_db()
        assert profile.weight_kg == 70.0
        assert profile.height_cm == 165
        assert profile.age == 40

    def test_a_dry_run_says_it_changed_nothing(self):
        # Профиль нужен: без строк команда честно отвечает «стирать
        # нечего», и предупреждение про --apply не печатается вовсе.
        _profile("purge-dry-says")

        out = _run()

        assert "Ничего не изменено" in out
        assert "--apply" in out


class TestValuesArePrintedNotCounted:
    def test_the_report_carries_the_values_themselves(self):
        """§120 дословно: печатаются значения, а не счётчик.

        «Удалили четыре» — утверждение о числе строк; «удалили 95.0» — о
        том, что именно ушло. Проверять можно только второе.
        """
        _profile("purge-values", weight_kg=95.0, height_cm=185, age=41)

        out = _run()

        assert "95.0" in out
        assert "185" in out
        assert "41" in out

    def test_the_values_are_printed_before_deletion_not_after(self):
        """Печать ДО удаления — иначе значений уже неоткуда взять.

        Проверяется на боевом порядке: с `--apply` отчёт всё равно несёт
        стёртые значения, потому что снят до записи.
        """
        _profile("purge-order", weight_kg=90.0)

        out = _run("--apply")

        assert "90.0" in out
        assert "Стёрто профилей: 1" in out


class TestWhatIsErasedAndWhatIsNot:
    def test_apply_clears_the_three_named_columns(self):
        profile = _profile("purge-apply")

        _run("--apply")

        profile.refresh_from_db()
        assert profile.weight_kg is None
        assert profile.height_cm is None
        assert profile.age is None

    def test_the_fields_the_owner_kept_survive(self):
        """Положительная стража: команда стирает НЕ всё.

        Без неё все тесты выше зеленели бы и на команде, которая чистит
        профиль целиком, — то есть на потере данных вместо исполнения
        решения. `gender`, `activity_coefficient` и `goal` владелец
        оставил явно.
        """
        profile = _profile("purge-keeps")

        _run("--apply")

        profile.refresh_from_db()
        assert profile.gender == "female"
        assert profile.activity_coefficient == 1.4
        assert profile.goal == "maintain"

    def test_the_input_snapshot_loses_the_same_three_keys(self):
        """Второе место, где вес пережил бы удаление.

        `targets_input_snapshot` хранит те же параметры под теми же
        именами. На пилоте он сегодня пуст, но пусто сегодня не значит
        пусто в день запуска — поэтому чистится, а не игнорируется.
        """
        profile = _profile(
            "purge-snapshot",
            targets_input_snapshot={
                "weight_kg": 70.0, "height_cm": 165, "age": 40,
                "gender": "female", "goal": "maintain",
            },
        )

        _run("--apply")

        profile.refresh_from_db()
        snap = profile.targets_input_snapshot
        assert "weight_kg" not in snap
        assert "height_cm" not in snap
        assert "age" not in snap
        # Ключи вне объёма §120 остаются — тот же довод, что и о столбцах.
        assert snap["gender"] == "female"
        assert snap["goal"] == "maintain"

    def test_the_derived_numbers_are_left_alone_and_said_so(self):
        """Команда печатает то, чего НЕ сделала.

        `bmr` и `daily_kcal` посчитаны от стираемых параметров и после
        удаления переживут свои входы. Объём §120 их не называет, и
        расширять его молча нельзя — но и умолчать нельзя: «параметры
        тела удалены» прочиталось бы как «от тела ничего не осталось».

        Это не гипотеза: два профиля пилота уже в этом состоянии — вес
        пуст, ориентиры заполнены.
        """
        profile = _profile("purge-derived")

        out = _run("--apply")

        profile.refresh_from_db()
        assert profile.bmr == 1392
        assert profile.daily_kcal == 1590
        assert "ОСТАЁТСЯ" in out
        assert "bmr=1392" in out


class TestRowsWithoutBodyParametersAreNotTouched:
    def test_a_profile_without_them_is_not_reported(self):
        """Профиль без параметров тела в отчёт не попадает.

        Иначе счётчик «затронуто» раздувался бы строками, где стирать
        нечего, и отчёт перестал бы отвечать на вопрос «сколько людей».
        """
        _profile(
            "purge-empty",
            weight_kg=None, height_cm=None, age=None,
        )

        out = _run()

        assert "Стирать нечего" in out


class TestTheOrderOfExecutionIsAConditionNotAWish:
    """§103 очищает ориентиры, §120 стирает входы — и только в таком порядке.

    В обратном возникает окно, где входы стёрты, а ориентиры
    показываются: ориентир как актуальный без происхождения, что §92
    правило 5 запрещает прямо.

    Окно не гипотетическое — два профиля пилота уже в нём. Условие,
    записанное в тело PR, через месяц прочитает не тот, кто запускает,
    поэтому оно стоит в коде и отказывает.
    """

    def test_apply_refuses_while_the_targets_still_have_provenance(self):
        from django.core.management.base import CommandError

        profile = _profile(
            "purge-too-early",
            targets_source=NutritionProfile.TargetsSource.UNKNOWN_LEGACY,
        )

        with pytest.raises(CommandError) as exc:
            _run("--apply")

        assert "Сначала N-b" in str(exc.value)
        # И ГЛАВНОЕ: отказ означает, что не стёрто ничего.
        profile.refresh_from_db()
        assert profile.weight_kg == 70.0

    def test_the_dry_run_says_it_is_too_early_instead_of_refusing(self):
        """Сухой прогон не отказывает — он предупреждает.

        Отказать ему значило бы отнять у человека возможность посмотреть,
        что БУДЕТ стёрто, до того как порядок соблюдён. Отчёт безопасен
        по построению; запрет нужен только необратимому действию.
        """
        _profile(
            "purge-dry-too-early",
            targets_source=NutritionProfile.TargetsSource.UNKNOWN_LEGACY,
        )

        out = _run()

        assert "СЕЙЧАС ЗАПУСКАТЬ РАНО" in out
        assert "70.0" in out  # значения всё равно показаны

    def test_after_n_b_the_same_call_goes_through(self):
        """Положительная стража: сторож порядка закрывает НЕ навсегда.

        Без неё тесты выше зеленели бы и на команде, которая отказывает
        всегда, — то есть на неисполнимом решении §120 вместо
        отложенного.
        """
        profile = _profile(
            "purge-after-nb",
            targets_source=NutritionProfile.TargetsSource.NONE,
        )

        _run("--apply")

        profile.refresh_from_db()
        assert profile.weight_kg is None
