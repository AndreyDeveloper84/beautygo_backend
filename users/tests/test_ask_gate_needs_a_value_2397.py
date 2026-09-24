"""Вывод, который ничего не нашёл, — не «уже знаем» (DRF-2397).

Правило 5 движка молчания читало ТОЛЬКО пометку происхождения:
`data_sources[field] in {explicit, inferred, erased}` → вопрос закрыт. Для
`inferred` это неверно, и не в теории: `_infer_favorite_masters` ставит
пометку БЕЗУСЛОВНО — тем, что вернул запрос. У человека, у которого ни с
одним мастером нет трёх завершённых записей, это пустой список. Значит
первый же ночной проход закрывал «Есть любимый мастер, к кому вернуться?»
навсегда: вывод не нашёл ничего, а человека не спросили ни разу.

`explicit` — другой случай, и пустота там ОТВЕТ. Приложение ставит эту
пометку всякому полю из тела PATCH, каким бы оно ни было
(`personal_context_views`): снятая галочка «дни, которые лучше избегать» —
это сказанное «нет таких», и переспрашивать его каждые сутки нельзя. Так же
выглядит отмена прежнего ответа в боте («я теперь снова ем мясо» — «диеты
нет», `orchestrator/memory/ayla_bridge.py` в ai-bot-platform).

Третий случай пустого `explicit` — просьба забыть одно поле
(`clear_declared_fields` там же). Молчание верно и по ней, но по ДРУГОЙ
причине: по решению DRF-1366, а не потому что пустой ответ — ответ. Честная
пометка для просьбы — `erased`, и внутренний PATCH её не принимает; пока
контракт не научится её выражать, различить «ответил пусто» и «просил
забыть» в строке нечем. Это отложено, а не решено.

* h1 — `explicit` со значением молчит (как было);
* h2 — `explicit` без значения молчит: пустой ответ — ответ;
* h3 — `inferred` без значения СПРАШИВАЕТ: заявлять знание не на чем;
* h4 — `inferred` со значением молчит (как было);
* h5 — `erased` молчит и на пустом: это решение DRF-1366;
* h6 — пометки нет вовсе → прежнее поведение;
* h7 — имя вне модели знания не заявляет (и не роняет запрос);
* h8 — живая форма дефекта целиком: ночной проход по короткой истории →
  `favorite_masters=[]` с пометкой `inferred` → вопрос остаётся открытым;
* h9 — названная цена правки: у `_infer_busy_days` пустой список ПОСЛЕ
  порога истории — это вывод «избегать нечего», и такому человеку вопрос
  снова откроется. Читатель два этих случая не различает.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, UserPersonalContext
from users.personal_context_erasure import ERASED
from users.personal_context_inference import (
    BUSY_DAYS_MIN_HISTORY,
    FAVORITE_MIN_COMPLETED,
    infer_for_user,
)
from users.personalization_engine import should_ask_question

pytestmark = pytest.mark.django_db

FIELD = "diet_type"


def _person(n: int, **context_kwargs):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username=f"ask2397_{n}",
        password="x",
        phone=f"+799922240{n:02d}",
    )
    user.onboarding_completed = True
    user.save(update_fields=["onboarding_completed"])
    UserPersonalContext.objects.create(user=user, **context_kwargs)
    return user


def _book_days(user, *, suffix: str, days: int):
    """``days`` завершённых записей подряд, начиная с понедельника.

    Подряд — чтобы число дней прямо задавало, какие дни недели заняты: восемь
    дней подряд покрывают все семь.
    """
    spec_user = type(user).objects.create_user(
        username=f"ask2397_spec{suffix}", password="x", role="specialist",
        phone=f"+7999222409{suffix}",
    )
    spec, _ = SpecialistProfile.objects.get_or_create(
        user=spec_user, defaults={"display_name": "Master", "bio": "t"},
    )
    cat, _ = ServiceCategory.objects.get_or_create(
        slug="ask2397-cat", defaults={"name": "Cat"},
    )
    service = Service.objects.create(
        specialist=spec, category=cat, name="Svc", price=1000, duration_minutes=60,
    )
    monday = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)
    for i in range(days):
        ts = monday + timedelta(days=i)
        Appointment.objects.create(
            client=user, specialist=spec, service=service,
            start_datetime=ts, end_datetime=ts + timedelta(hours=1),
            price=1000, status=Appointment.Status.COMPLETED,
        )
    return spec


class TestH1AStampedValueClosesTheQuestion:
    def test_a_named_value_is_not_asked_again(self) -> None:
        user = _person(1, diet_type="vegan", data_sources={FIELD: "explicit"})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"


class TestH2AnEmptyAnswerIsStillAnAnswer:
    def test_an_empty_explicit_field_stays_silent(self) -> None:
        """Так выглядит снятая в приложении галочка и отмена прежнего ответа
        («снова ем мясо»). Спрашивать снова — не слышать сказанного. Тем же
        видом приходит просьба забыть одно поле: по ней молчание тоже верно,
        но по решению DRF-1366, и различить их в строке пока нечем."""
        user = _person(2, diet_type="", data_sources={FIELD: "explicit"})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"


class TestH3ADerivationThatFoundNothingIsNotKnowledge:
    def test_an_empty_inferred_field_is_asked(self) -> None:
        user = _person(3, favorite_masters=[], data_sources={"favorite_masters": "inferred"})

        verdict = should_ask_question(user, "favorite_masters")

        assert verdict.allowed is True
        assert verdict.reason == "ok"


class TestH4ADerivationWithAValueStaysSilent:
    def test_a_filled_inferred_field_is_not_asked(self) -> None:
        user = _person(4, busy_days=["sat"], data_sources={"busy_days": "inferred"})

        verdict = should_ask_question(user, "busy_days")

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"


class TestH5ErasedStaysSilentByDecision:
    def test_a_tombstone_closes_the_question_even_though_the_field_is_empty(self) -> None:
        """DRF-1366: кто сказал «забудь всё», не должен быть допрошен на
        следующем ходу. Здесь пустота — не незнание, а просьба человека."""
        user = _person(5, diet_type="", data_sources={FIELD: ERASED})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"


class TestH6NoStampIsUnchanged:
    def test_an_unstamped_field_is_asked(self) -> None:
        user = _person(6, diet_type="vegan", data_sources={})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is True


class TestH7AnUndeclaredNameCarriesNoKnowledge:
    def test_a_stamp_on_a_name_that_is_not_a_field_claims_nothing(self) -> None:
        """Мерило значения — умолчание модели, и у имени вне модели его нет.
        Без проверки членства это имя роняет запрос на ``getattr``
        (``AttributeError``); имя, которое у строки есть, но полем о человеке
        не является, не роняет ничего — и тем опаснее. Правило 5 в обоих
        случаях знания не заявляет."""
        user = _person(7, data_sources={"not_a_field_at_all": "inferred"})

        verdict = should_ask_question(user, "not_a_field_at_all")

        assert verdict.allowed is True
        assert verdict.reason == "ok"


class TestH8TheLiveShapeOfTheDefect:
    """Не выдуманная строка, а то, что делает ночной проход сам.

    Человек с двумя завершёнными записями у одного мастера: порог
    ``FAVORITE_MIN_COMPLETED`` не пройден, выводить нечего — и проход всё
    равно ставит пометку `inferred` на пустой список. Вопрос про любимого
    мастера ему ещё ни разу не задавали.
    """

    def test_a_pass_that_found_nothing_leaves_the_question_open(self) -> None:
        user = _person(8)
        _book_days(user, suffix="8", days=FAVORITE_MIN_COMPLETED - 1)  # на один меньше порога

        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        # Сначала утверждение о НАЛИЧИИ: проход действительно проштамповал поле.
        assert ctx.data_sources["favorite_masters"] == "inferred"
        assert ctx.favorite_masters == []
        verdict = should_ask_question(user, "favorite_masters")
        assert verdict.allowed is True


class TestH9TheNamedPriceOfThisRule:
    """Обратная сторона той же монеты, названная вслух.

    У `_infer_busy_days` есть порог истории, и пустой список ПОСЛЕ порога —
    содержательный вывод «избегать нечего» (человек записывался во все семь
    дней недели). Для строки он выглядит так же, как вывод, не нашедший
    ничего, — различает их только писатель. Значит такому человеку вопрос про
    занятые дни снова откроется, пока он на него не ответит: это цена правки,
    а не её цель, и узел стоит здесь, чтобы её не «исправили» молча.
    """

    def test_a_conclusive_empty_derivation_reopens_the_question(self) -> None:
        user = _person(9)
        _book_days(user, suffix="9", days=BUSY_DAYS_MIN_HISTORY)  # все семь дней недели

        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.data_sources["busy_days"] == "inferred"
        assert ctx.busy_days == []
        assert should_ask_question(user, "busy_days").allowed is True
