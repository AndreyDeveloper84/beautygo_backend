"""«Уже знаем» — про значение, а не про ярлык (DRF-2397).

Правило 5 движка молчания (`personalization_engine`) читает ТОЛЬКО пометку
происхождения: `data_sources[field] in {explicit, inferred}` → вопрос закрыт.
Само поле при этом может быть пустым — и тогда «уже знаем» неправда: не знаем
ничего, а спрашивать больше не будем никогда.

Так бывает не в теории. Бот пишет пустое значение С пометкой в двух живых
местах: «я теперь снова ем мясо» (отказ от прежнего ответа) и «забудь про мою
диету» (стирание одного поля) — оба уходят как
`{"value": "", "source": "explicit"}`. То есть человек, попросивший ЗАБЫТЬ,
получает вопрос закрытым навсегда, и память о нём пуста.

Отдельно от этого стоит `erased`: он закрывает вопрос намеренно и по
документированному решению (DRF-1366 — «кто сказал „забудь всё“, не должен быть
допрошен на следующем ходу»), и этот узел его не трогает.

* h1 — пометка есть, значение есть → не спрашиваем (как было);
* h2 — пометка есть, значение ПУСТО → спрашиваем: «уже знаем» было бы ложью;
* h3 — `erased` закрывает вопрос и на пустом поле: это решение, а не побочный
  эффект;
* h4 — пометки нет вовсе → прежнее поведение не меняется.
"""
from __future__ import annotations

import pytest

from users.models import UserPersonalContext
from users.personal_context_erasure import ERASED
from users.personalization_engine import should_ask_question

pytestmark = pytest.mark.django_db

FIELD = "diet_type"


def _person(**context_kwargs):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username=f"ask2397{context_kwargs.pop('n', 1)}",
        password="x",
        phone=f"+7999222{context_kwargs.pop('p', '0001')}",
    )
    user.onboarding_completed = True
    user.save(update_fields=["onboarding_completed"])
    UserPersonalContext.objects.create(user=user, **context_kwargs)
    return user


class TestH1AStampedValueClosesTheQuestion:
    def test_a_named_value_is_not_asked_again(self) -> None:
        user = _person(n=1, p="0001", diet_type="vegan", data_sources={FIELD: "explicit"})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"


class TestH2AStampWithoutAValueIsNotKnowledge:
    def test_an_empty_field_is_still_asked(self) -> None:
        """Пустое значение с пометкой — это «не знаем ничего», а не «знаем».
        Так выглядит строка после «я снова ем мясо» и после «забудь про мою
        диету»: бот пишет пустое значение с пометкой `explicit`."""
        user = _person(n=2, p="0002", diet_type="", data_sources={FIELD: "explicit"})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is True

    def test_the_same_holds_for_the_inferred_stamp(self) -> None:
        user = _person(n=3, p="0003", diet_type="", data_sources={FIELD: "inferred"})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is True


class TestH3ErasedStaysSilentByDecision:
    def test_a_tombstone_closes_the_question_even_though_the_field_is_empty(self) -> None:
        """DRF-1366: кто сказал «забудь всё», не должен быть допрошен на
        следующем ходу. Здесь пустота — не незнание, а просьба человека."""
        user = _person(n=4, p="0004", diet_type="", data_sources={FIELD: ERASED})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"


class TestH4NoStampIsUnchanged:
    def test_an_unstamped_field_is_asked(self) -> None:
        user = _person(n=5, p="0005", diet_type="vegan", data_sources={})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is True


class TestH5AnUndeclaredNameCarriesNoKnowledge:
    def test_a_stamp_on_a_name_that_is_not_a_field_claims_nothing(self) -> None:
        """Мерило значения — умолчание модели, и у имени вне модели его нет.
        Правило 5 тогда не заявляет «уже знаем», а пропускает ход дальше:
        заявить знание о том, чего в строке нет вовсе, — та же ложь, что и
        пометка без значения."""
        user = _person(n=6, p="0006", data_sources={"not_a_field_at_all": "explicit"})

        verdict = should_ask_question(user, "not_a_field_at_all")

        assert verdict.allowed is True
        assert verdict.reason == "ok"
