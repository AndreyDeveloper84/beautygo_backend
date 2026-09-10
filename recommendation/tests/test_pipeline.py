"""Конвейер S0–S6 — контракт §3, §4, §8.4, §9.5.

Тесты написаны от нарушений, а не от happy path: почти каждый ловит
конкретный сегодняшний дефект, названный в аудите
`RECOMMENDATION_PATHS_AUDIT.md`.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from django.test import override_settings

from recommendation.api import (
    Constraint,
    MappingStatus,
    MatchLevel,
    NeedOrigin,
    NeedSpec,
    RankedCandidate,
    ReasonCode,
    SafetyState,
    ScheduleState,
    Scope,
    ScopeMode,
    StagePolicy,
    StageId,
    StageVerdict,
    Surface,
    UserConstraints,
    resolve,
)

from .conftest import StaticSource, make_facts, make_request


def _ranked_ids(decision) -> list[uuid.UUID]:
    return [c.candidate_ref.id for c in decision.ordered]


def _by_id(decision, cid) -> RankedCandidate:
    return next(c for c in decision.ordered if c.candidate_ref.id == cid)


# ---------------------------------------------------------------------------
# Лексикографический конвейер, а не сумма
# ---------------------------------------------------------------------------

def test_semantic_fit_outranks_quality():
    """R1: соответствие нужде раньше качества, и сумма их не смешивает.

    Сегодня движок весит `service_match` 0.15 против `rating` 0.30 — то
    есть качество перевешивает соответствие вдвое. Здесь этого не может
    произойти в принципе: S5 работает только внутри того, что не различил S2.
    """
    exact = make_facts(match_level=MatchLevel.SERVICE_EXACT, rating=("3.0", 50))
    partial = make_facts(match_level=MatchLevel.SERVICE_PARTIAL, rating=("5.0", 50))

    decision = resolve(
        make_request(),
        source=StaticSource([partial, exact]),
        policy=StagePolicy(n_substantiated=10),
    )

    assert _ranked_ids(decision)[0] == exact.ref.id
    assert _by_id(decision, exact.ref.id).tier < _by_id(decision, partial.ref.id).tier


def test_quality_orders_only_inside_a_tie():
    """S5 различает там, где S2–S4 промолчали, — и только там."""
    good = make_facts(rating=("5.0", 50))
    plain = make_facts(rating=("4.0", 50))

    decision = resolve(
        make_request(),
        source=StaticSource([plain, good]),
        policy=StagePolicy(n_substantiated=10),
    )

    assert _ranked_ids(decision)[0] == good.ref.id
    assert _by_id(decision, good.ref.id).stage_verdicts[StageId.S2] is StageVerdict.TIED
    assert _by_id(decision, good.ref.id).stage_verdicts[StageId.S5] is StageVerdict.DISTINGUISHED


# ---------------------------------------------------------------------------
# §8.4 E3–E4: рейтинг без отзывов
# ---------------------------------------------------------------------------

def test_unsubstantiated_rating_changes_nothing_when_replaced_by_zero(source_factory):
    """E3: позиция не меняется при подмене 4.9 на 0.0.

    Дословно случай пилота: литерал рейтинга, посаженный в сид ради порога
    чужого движка, не влияет на выдачу вообще.
    """
    other = make_facts(rating=("4.0", 50))
    loud = make_facts(rating=("4.9", 0))
    silent = make_facts(cid=loud.ref.id, rating=("0.0", 0))

    with_loud = resolve(make_request(), source=source_factory([other, loud]))
    with_silent = resolve(make_request(), source=source_factory([other, silent]))

    assert _ranked_ids(with_loud) == _ranked_ids(with_silent)


def test_unsubstantiated_rating_yields_only_the_ignored_code():
    """E3: ни одного `QUALITY_`-кода, кроме «учтено не было»."""
    facts = make_facts(rating=("4.9", 0))
    decision = resolve(make_request(), source=StaticSource([facts]))

    codes = set(_by_id(decision, facts.ref.id).reason_codes)
    quality = {c for c in codes if c.value.startswith("QUALITY_")}
    assert quality == {ReasonCode.QUALITY_RATING_UNSUBSTANTIATED_IGNORED}


def test_candidate_without_reviews_is_not_demoted():
    """E4 и решение владельца §29.4: отсутствие данных не наказывается.

    Кандидат без отзывов остаётся в том же ярусе, что и кандидат с
    подтверждённой оценкой при равных S2–S4. Стадия качества про такую
    группу молчит целиком — иначе «не понижать» неисполнимо: дать
    неоценённому ключ 0 значит отправить его ярусом ниже.
    """
    confirmed = make_facts(rating=("5.0", 50))
    unrated = make_facts(rating=("4.9", 0))

    decision = resolve(
        make_request(),
        source=StaticSource([confirmed, unrated]),
        policy=StagePolicy(n_substantiated=10),
    )

    assert _by_id(decision, unrated.ref.id).tier == _by_id(decision, confirmed.ref.id).tier
    assert _by_id(decision, unrated.ref.id).stage_verdicts[StageId.S5] is StageVerdict.INACTIVE


def test_unsubstantiated_evidence_is_still_delivered():
    """Свидетельство передаётся — скрывать его контракт запрещает (§8.3)."""
    facts = make_facts(rating=("4.9", 0))
    decision = resolve(make_request(), source=StaticSource([facts]))

    ratings = [e for e in _by_id(decision, facts.ref.id).evidence if e.kind.value == "RATING"]
    assert ratings and ratings[0].value.review_count == 0


# ---------------------------------------------------------------------------
# Выход: ни строки для показа, ни сырого балла
# ---------------------------------------------------------------------------

def test_output_carries_no_display_string_and_no_score():
    """W1 / канон §8: наружу идут ярусы, коды и evidence. И больше ничего.

    Проверка по полям типа, а не по одному ответу: поле, добавленное
    завтра, сломает этот тест сегодняшним запуском.
    """
    field_names = set(RankedCandidate.__dataclass_fields__)
    assert not field_names & {"score", "reasoning_text", "reason_text", "why_text", "match_score"}
    assert {"tier", "rank", "reason_codes", "evidence"} <= field_names


# ---------------------------------------------------------------------------
# S0 / S1 — допустимость
# ---------------------------------------------------------------------------

def test_geo_unknown_is_excluded_under_explicit_radius_not_scored_as_half():
    """§3.3: неизвестная география — не «полбалла».

    Сегодня `_score_distance` возвращает при `None` ровно 0.5 и пускает
    неизмеренную географию в ранжирование наравне с измеренной.
    """
    near = make_facts(distance_km=1.0)
    unknown = make_facts(distance_km=None)

    decision = resolve(
        make_request(scope=Scope(ScopeMode.MARKETPLACE, city="Москва", radius_km=5.0)),
        source=StaticSource([near, unknown]),
    )

    assert _ranked_ids(decision) == [near.ref.id]
    assert decision.excluded[0].reason_code is ReasonCode.SCOPE_GEO_UNKNOWN_EXCLUDED


def test_city_is_a_filter_never_a_score():
    """Канон §9.1: город отсекает, а не доранжирует."""
    here = make_facts(city="Москва")
    elsewhere = make_facts(city="Лион")

    decision = resolve(
        make_request(scope=Scope(ScopeMode.MARKETPLACE, city="Москва")),
        source=StaticSource([here, elsewhere]),
    )

    assert _ranked_ids(decision) == [here.ref.id]
    assert ReasonCode.SCOPE_WITHIN_CITY in _by_id(decision, here.ref.id).reason_codes


def test_unmapped_catalog_is_not_recommendable():
    """§10.1: `catalog_visible ≠ recommendation_eligible`, и `UNKNOWN` — не «да»."""
    decision = resolve(
        make_request(),
        source=StaticSource([make_facts(mapping_status=MappingStatus.UNMAPPED)]),
    )

    assert decision.is_empty
    assert decision.excluded[0].reason_code is ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE
    assert ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE in decision.reason_codes


@pytest.mark.parametrize(
    "status",
    [MappingStatus.UNMAPPED, MappingStatus.REVIEW_REQUIRED, MappingStatus.UNKNOWN],
)
@override_settings(RECOMMENDATION_PILOT_MAPPING_OVERRIDE=True)
def test_nothing_but_verified_is_admitted_whatever_the_settings_say(status):
    """§76: ноль `VERIFIED` не разрешает fallback — и обойти это нечем.

    Здесь стоял тест про пилотное исключение: политика с
    `mapping_override_enabled=True` пропускала кандидата с любым статусом,
    помечая его `UNSUBSTANTIATED`. Это было честно ровно до тех пор, пока
    вопрос владельца оставался открытым. Он ответил: «иначе статус будет
    декоративным, а система продолжит выдавать непроверенные связи».

    Настройка выставлена **нарочно** и обязана ничего не изменить: она
    больше не существует, и тест это утверждает, а не подразумевает.
    Без неё проверка доказывала бы только то, что при выключенном флаге
    всё как раньше, — то есть ничего.

    `REVIEW_REQUIRED` здесь важнее остальных: именно его получат все 206
    связей пилота после миграции, и именно его владелец запретил считать
    достаточным.
    """
    decision = resolve(
        make_request(),
        source=StaticSource([make_facts(mapping_status=status)]),
    )

    assert decision.is_empty
    assert decision.excluded[0].reason_code is ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE


def test_verified_is_still_admitted():
    """Положительная стража: правило не выродилось в «не пускать никого».

    Без неё три случая выше зеленели бы и на коде, который отвергает
    всех подряд, — а тогда шкала владельца была бы бессмысленной, и
    подтверждать связи не имело бы смысла.
    """
    facts = make_facts(mapping_status=MappingStatus.VERIFIED)
    decision = resolve(make_request(), source=StaticSource([facts]))

    assert _ranked_ids(decision) == [facts.ref.id]
    mapping = [
        e for e in _by_id(decision, facts.ref.id).evidence
        if e.kind.value == "CAPABILITY_MAPPING"
    ]
    assert mapping and mapping[0].strength.value == "CONFIRMED"


def test_safety_unknown_is_fail_closed_like_stop():
    """§14: «мы не знаем, безопасно ли» не является разрешением."""
    for state in (SafetyState.STOP, SafetyState.UNKNOWN):
        decision = resolve(
            make_request(safety_state=state),
            source=StaticSource([make_facts()]),
        )
        assert decision.is_empty
        assert decision.excluded[0].reason_code is ReasonCode.ELIG_EXCLUDED_SAFETY


def test_budget_that_cannot_be_confirmed_does_not_pass():
    """§3.4 + fail-closed: жёсткое ограничение не удовлетворяется неизвестностью."""
    priced = make_facts(price=Decimal("2500"))
    unknown_price = make_facts(price=None)

    decision = resolve(
        make_request(constraints=UserConstraints(price_max=Constraint.known(Decimal("3000")))),
        source=StaticSource([priced, unknown_price]),
    )

    assert _ranked_ids(decision) == [priced.ref.id]
    assert ReasonCode.ELIG_WITHIN_STATED_BUDGET in _by_id(decision, priced.ref.id).reason_codes


# ---------------------------------------------------------------------------
# Ярусы, ротация, «Показать ещё»
# ---------------------------------------------------------------------------

def test_indistinguishable_candidates_share_a_tier():
    """§29.3: равным — равный ярус, а не случайный «лучший»."""
    a, b, c = make_facts(), make_facts(), make_facts()
    decision = resolve(make_request(), source=StaticSource([a, b, c]))

    assert {cand.tier for cand in decision.ordered} == {1}
    for cand in decision.ordered:
        assert ReasonCode.TIE_TIER_SHARED in cand.reason_codes
        assert ReasonCode.TIE_ROTATION_APPLIED in cand.reason_codes


def test_no_truncation_inside_the_resolver():
    """§12.2: отсечение не удаляет кандидатов навсегда.

    `k` — сколько будет ПОКАЗАНО, а не сколько считать. Резолвер отдаёт
    всё допустимое множество; срез делает поверхность, и делает его после
    ротации. Иначе «Показать ещё» показывать нечего.
    """
    candidates = [make_facts() for _ in range(7)]
    decision = resolve(make_request(k=3), source=StaticSource(candidates))
    assert len(decision.ordered) == 7


def test_rotation_cannot_overtake_a_stage_that_distinguished():
    """§12.2 и позитивная стража DRF-1411: ротация — не переранжирование."""
    exact = make_facts(match_level=MatchLevel.SERVICE_EXACT)
    partial = make_facts(match_level=MatchLevel.SERVICE_PARTIAL)

    for seed in (f"conv-{i}" for i in range(30)):
        decision = resolve(make_request(seed=seed), source=StaticSource([partial, exact]))
        assert _ranked_ids(decision)[0] == exact.ref.id


def test_source_order_does_not_reach_output():
    """Источник отдаёт множество, а не порядок.

    Доживи его порядок до выдачи — он влиял бы на то, что видит человек,
    оставаясь «просто источником»: четвёртый авторитет, самый незаметный.
    """
    candidates = [make_facts() for _ in range(6)]
    straight = resolve(make_request(seed=None), source=StaticSource(candidates))
    reversed_ = resolve(make_request(seed=None), source=StaticSource(list(reversed(candidates))))
    assert _ranked_ids(straight) == _ranked_ids(reversed_)


# ---------------------------------------------------------------------------
# §9.5 — один запрос, одно решение, независимо от поверхности
# ---------------------------------------------------------------------------

def test_same_request_gives_same_decision_across_surfaces():
    """Инвариант SURFACE-INDEPENDENT RECOMMENDATION.

    Пока этот тест зелёный, вторая политика невозможна по построению:
    поверхность не может ни переставить кандидатов, ни поменять коды.
    """
    candidates = [make_facts(rating=("4.9", 0)) for _ in range(5)]

    bot = resolve(make_request(surface=Surface.BOT_CHAT), source=StaticSource(candidates))
    home = resolve(make_request(surface=Surface.MINIAPP_HOME), source=StaticSource(candidates))

    assert _ranked_ids(bot) == _ranked_ids(home)
    assert [c.tier for c in bot.ordered] == [c.tier for c in home.ordered]
    assert [c.reason_codes for c in bot.ordered] == [c.reason_codes for c in home.ordered]


# ---------------------------------------------------------------------------
# Стадии без данных
# ---------------------------------------------------------------------------

def test_stage_without_data_is_inactive_not_zero():
    """R4: стадия без данных объявляется `INACTIVE` и никого не двигает."""
    decision = resolve(
        make_request(need=NeedSpec(origin=NeedOrigin.USER_EXPLICIT)),
        source=StaticSource([make_facts(), make_facts()]),
    )

    activity = {row.stage: row for row in decision.stage_activity}
    assert activity[StageId.S2].active is False
    assert activity[StageId.S2].reason
    assert activity[StageId.S4].active is False
    assert activity[StageId.S5].active is False
    assert ReasonCode.QUALITY_NO_EVIDENCE in decision.reason_codes


def test_provider_without_resolved_service_is_not_admitted_to_a_stated_need():
    """K2 плюс уточнение S1: не отвечающий названной нужде не показывается.

    Сегодняшний дефект поверхности B дословно: провайдеры упорядочены как
    будто по соответствию нужде, а соответствие не вычислялось. Раньше
    такой кандидат оставался в выдаче ниже по списку; теперь при ЯВНО
    названной нужде он выбывает на S1.

    Причина не в строгости. Показать его значило бы молча подставить
    другую услугу (канон §14.4), а «непустой недоказанный ответ» хуже
    пустого честного (§14): человек, набравший «массаж», должен получить
    массаж или честное «никого», но не маникюр ниже по списку.

    Положительная стража на тех же данных обязательна: без неё тест
    зеленел бы и на коде, который не пускает вообще никого.
    """
    resolved = make_facts(match_level=MatchLevel.SERVICE_EXACT)
    unresolved = make_facts(match_level=MatchLevel.SERVICE_EXACT, matched_service_ref=None)

    decision = resolve(make_request(), source=StaticSource([unresolved, resolved]))

    assert _ranked_ids(decision) == [resolved.ref.id]
    assert [e.reason_code for e in decision.excluded] == [ReasonCode.ELIG_EXCLUDED_NOT_CAPABLE]


def test_unresolved_provider_stays_when_the_need_was_never_stated():
    """Нужда не названа — исключать не за что.

    Отрицательная половина предыдущего теста: правило срабатывает ТОЛЬКО
    когда человек сказал, что ищет. Молчание не является требованием,
    и отсутствие соответствия при молчании — не «не подходит», а
    «не вычислялось» (R4).
    """
    unresolved = make_facts(match_level=MatchLevel.SERVICE_EXACT, matched_service_ref=None)

    decision = resolve(
        make_request(need=NeedSpec(origin=NeedOrigin.USER_EXPLICIT)),
        source=StaticSource([unresolved]),
    )

    assert _ranked_ids(decision) == [unresolved.ref.id]
    assert decision.excluded == ()


def test_unconfirmed_schedule_is_barred_from_the_first_tier():
    """K5 / §29.5: неподтверждённое расписание — не «свободен» и не «занят».

    Кандидат остаётся в выдаче, но первым ярусом не становится. Когда
    подтверждённых нет вовсе, первый ярус остаётся пустым: сказать
    «первого нет» честнее, чем назначить первым того, чьё расписание
    никто не подтверждал.
    """
    confirmed = make_facts(is_bookable=True, schedule_state=ScheduleState.CONFIRMED_IN_WINDOW)
    unconfirmed = make_facts(is_bookable=True, schedule_state=ScheduleState.UNCONFIRMED)
    request = make_request(constraints=UserConstraints(time_window=Constraint.known("today")))
    policy = StagePolicy(availability_ranking_enabled=True)

    decision = resolve(request, source=StaticSource([unconfirmed, confirmed]), policy=policy)

    assert _by_id(decision, confirmed.ref.id).tier < _by_id(decision, unconfirmed.ref.id).tier
    assert ReasonCode.EXEC_SCHEDULE_UNCONFIRMED in _by_id(decision, unconfirmed.ref.id).reason_codes

    only_unconfirmed = resolve(request, source=StaticSource([unconfirmed]), policy=policy)
    assert only_unconfirmed.ordered[0].tier == 2


def test_availability_stays_inactive_until_the_data_condition_is_met():
    """K6: признак доступности включается, только когда под ним появились данные."""
    facts = make_facts(schedule_state=ScheduleState.UNCONFIRMED)
    decision = resolve(
        make_request(constraints=UserConstraints(time_window=Constraint.known("today"))),
        source=StaticSource([facts]),
    )

    codes = set(_by_id(decision, facts.ref.id).reason_codes)
    assert ReasonCode.EXEC_SCHEDULE_UNCONFIRMED not in codes


def test_census_counts_everyone_seen_not_only_survivors():
    """§76: числа `REVIEW_REQUIRED` и `UNMAPPED` — то, по чему видно движение.

    На пилоте это не диагностика края, а постоянное состояние: после
    миграции `review_required 206 · unmapped 59 · verified 0`. Пока
    подтверждённых нет, единственный признак прогресса — переезд числа
    из одной колонки в другую.

    Считается **увиденное**, а не выжившее. Кандидат, выбывший раньше
    гейта связи, свой статус всё равно имеет, и схлопнуть его значило бы
    потерять из счёта ровно тех, из-за кого счёт и завели.
    """
    decision = resolve(
        make_request(),
        source=StaticSource([
            make_facts(mapping_status=MappingStatus.REVIEW_REQUIRED),
            make_facts(mapping_status=MappingStatus.REVIEW_REQUIRED),
            make_facts(mapping_status=MappingStatus.UNMAPPED),
            make_facts(mapping_status=MappingStatus.VERIFIED),
        ]),
    )

    census = decision.census
    assert (census.visible, census.recommendation_eligible) == (4, 1)
    assert (census.review_required, census.unmapped) == (2, 1)


def test_census_counts_a_candidate_dropped_before_the_mapping_gate():
    """Выбывший по безопасности из переписи НЕ исчезает.

    Иначе при `STOP` числа обнулялись бы целиком, и дежурный прочитал бы
    «непроверенных связей нет» там, где их полный каталог. Пустота
    получила бы второе объяснение, и оба выглядели бы одинаково.
    """
    decision = resolve(
        make_request(safety_state=SafetyState.STOP),
        source=StaticSource([make_facts(mapping_status=MappingStatus.REVIEW_REQUIRED)]),
    )

    assert decision.is_empty
    assert decision.census.visible == 1
    assert decision.census.review_required == 1
    assert decision.census.recommendation_eligible == 0


def test_decision_carries_policy_versions():
    """§6.3: без версий решение невоспроизводимо задним числом."""
    decision = resolve(make_request(), source=StaticSource([make_facts()]))
    versions = decision.policy_versions
    assert versions.resolver_spec_version
    assert versions.stage_policy_version
    assert versions.reason_code_registry_version
    assert versions.tie_break_policy_version


# ---------------------------------------------------------------------------
# Третий исход §93 на гейте и в переписи
# ---------------------------------------------------------------------------


def test_refused_candidate_is_not_admitted():
    """`NOT_RECOMMENDABLE` в подбор не проходит — как и всё, кроме `VERIFIED`.

    Поведение на гейте у отказа и у отсутствия одинаковое, и это
    правильно: гейт спрашивает «подтверждена ли связь», а не «почему
    нет». Различаются они дальше — в переписи, см. тест ниже.
    """
    decision = resolve(
        make_request(),
        source=StaticSource([make_facts(mapping_status=MappingStatus.NOT_RECOMMENDABLE)]),
    )

    assert decision.is_empty
    assert decision.excluded[0].reason_code is ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE


def test_refusal_is_counted_apart_from_absence():
    """Отказ считается СВОЕЙ колонкой, а не приплюсовывается к `unmapped`.

    Это и есть весь смысл четвёртого состояния. Гейт ведёт себя с ними
    одинаково, поэтому единственное место, где разница наблюдаема, —
    перепись. Схлопнутся здесь — состояние станет декоративным: очередь
    разбора перестанет убывать, потому что разобранные строки будут
    возвращаться в неё каждым отчётом.
    """
    decision = resolve(
        make_request(),
        source=StaticSource([
            make_facts(mapping_status=MappingStatus.UNMAPPED),
            make_facts(mapping_status=MappingStatus.NOT_RECOMMENDABLE),
            make_facts(mapping_status=MappingStatus.NOT_RECOMMENDABLE),
        ]),
    )

    census = decision.census
    assert census.visible == 3
    assert census.unmapped == 1, "отказ посчитан как отсутствие"
    assert census.not_recommendable == 2


def test_the_exclusion_code_is_not_the_status():
    """`ELIG_EXCLUDED_NOT_RECOMMENDABLE` — не имя состояния, а имя отказа гейта.

    Совпадение слов опасное и появилось раньше состояния: код был
    заведён как «не прошёл гейт связи» и достаётся ЛЮБОМУ статусу,
    кроме `VERIFIED`. Читатель, встретивший его в логе, не вправе
    заключить, что строка помечена `NOT_RECOMMENDABLE`.

    Тест закрепляет именно это: один и тот же код у трёх разных причин.
    Если код когда-нибудь переименуют в статус-специфичный, здесь
    станет красно, и переименование не пройдёт молча.
    """
    for status in (
        MappingStatus.UNMAPPED,
        MappingStatus.REVIEW_REQUIRED,
        MappingStatus.NOT_RECOMMENDABLE,
    ):
        decision = resolve(
            make_request(),
            source=StaticSource([make_facts(mapping_status=status)]),
        )
        assert (
            decision.excluded[0].reason_code
            is ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE
        ), f"{status} получил другой код отказа"
