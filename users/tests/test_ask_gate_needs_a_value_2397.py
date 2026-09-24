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
это сказанное «нет таких», и переспрашивать его каждые сутки нельзя. Бот
пишет пустое значение так же — отменяя прежний ответ («я теперь снова ем
мясо» — «диеты нет») и по просьбе забыть одно поле. Все три — решения
человека, молчание по ним верно.

* h1 — `explicit` со значением молчит (как было);
* h2 — `explicit` без значения молчит: пустой ответ — ответ;
* h3 — `inferred` без значения СПРАШИВАЕТ: заявлять знание не на чем;
* h4 — `inferred` со значением молчит (как было);
* h5 — `erased` молчит и на пустом: это решение DRF-1366;
* h6 — пометки нет вовсе → прежнее поведение;
* h7 — имя вне модели знания не заявляет (и не роняет запрос);
* h8 — живая форма дефекта целиком: ночной проход по короткой истории →
  `favorite_masters=[]` с пометкой `inferred` → вопрос остаётся открытым.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, UserPersonalContext
from users.personal_context_erasure import ERASED
from users.personal_context_inference import FAVORITE_MIN_COMPLETED, infer_for_user
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


class TestH1AStampedValueClosesTheQuestion:
    def test_a_named_value_is_not_asked_again(self) -> None:
        user = _person(1, diet_type="vegan", data_sources={FIELD: "explicit"})

        verdict = should_ask_question(user, FIELD)

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"


class TestH2AnEmptyAnswerIsStillAnAnswer:
    def test_an_empty_explicit_field_stays_silent(self) -> None:
        """Так выглядит и снятая в приложении галочка, и «снова ем мясо», и
        просьба забыть одно поле. Спрашивать снова — не слышать сказанного."""
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
        """Мерило значения — умолчание модели, и у имени вне модели его нет
        (``default_for`` на таком имени поднимает ``FieldDoesNotExist``).
        Правило 5 не заявляет знания и не роняет запрос."""
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
        spec_user = type(user).objects.create_user(
            username="ask2397_spec", password="x", role="specialist",
            phone="+79992224099",
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
        base = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)
        for i in range(FAVORITE_MIN_COMPLETED - 1):  # на один меньше порога
            ts = base + timedelta(days=i)
            Appointment.objects.create(
                client=user, specialist=spec, service=service,
                start_datetime=ts, end_datetime=ts + timedelta(hours=1),
                price=1000, status=Appointment.Status.COMPLETED,
            )

        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        # Сначала утверждение о НАЛИЧИИ: проход действительно проштамповал поле.
        assert ctx.data_sources["favorite_masters"] == "inferred"
        assert ctx.favorite_masters == []
        verdict = should_ask_question(user, "favorite_masters")
        assert verdict.allowed is True
