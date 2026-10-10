"""Обход всех читающих ручек каталога: засеянная синтетика не видна никому без разрешения.

Условие перед засевом на стенд: «фильтр в подборе слит» не равно «синтетика
не видна ни в одной клиентской ручке». Перечень ручек здесь не составлен
руками — он снят с маршрутов приложения, а ответ каждой ручки ищется на
следы засеянного набора. Что вернулось, то и есть перечень.

Узел засевает набор штатной командой (модулем засева рядом с командой), затем
шлёт GET на каждый маршрут — анонимно, обычным клиентом, тестовой персоной и
внутренним сервисным токеном, с параметрами и без — и требует, чтобы ни в
одном ответе не было ни названий, ни ключей, ни идентификаторов набора.

Настройки стенда при этом ВЫКЛЮЧЕНЫ: разрешения нет ни у кого, в том числе
у тестовой персоны. Это состояние стенда между засевом и включением.

**Чего обход не видит — читать вместе с числом «исполнено»:**

* ручку, которая этим вызывающим отвечает отказом (нет нужного токена,
  второго фактора бота, прав салона), — она в счёте «не исполнено», и о ней
  узел не говорит ничего;
* маршруты с параметрами, для которых у набора нет значения (запись,
  платёж, беседа);
* запросы не методом GET.
"""

from __future__ import annotations

import re

import pytest
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APIClient

from services.models import (
    CapabilityGoalLink, GoalOption, ProcedureCapability, SalonService, ServiceCategory, ServiceTemplate,
    SpecialistService,
)
from services.management.commands._synthetic_plan_fixture import load_spec, seed
from tenants.models import Tenant
from users.models import User
from wellness.tests.plan_consent import ATTESTATION

pytestmark = pytest.mark.django_db

TOKEN = "synthetic-sweep-internal-token"
SKIPPED_PREFIXES = ("admin/", "api/schema", "api/docs", "api/redoc", "^media")


def _routes() -> list[str]:
    found: list[str] = []

    def walk(patterns, prefix: str = "") -> None:
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
            elif isinstance(pattern, URLPattern):
                found.append(prefix + str(pattern.pattern))

    walk(get_resolver().url_patterns)
    return sorted({
        route for route in found
        if not route.startswith(SKIPPED_PREFIXES) and "<format>" not in route and "(?P<format>" not in route
    })


@pytest.fixture
def seeded(settings, db, monkeypatch):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN
    # Обход шлёт тысячи запросов подряд: без этого ограничитель частоты
    # отвечал бы 429, и ручка выглядела бы «не исполненной» по чужой причине.
    from rest_framework.throttling import BaseThrottle, SimpleRateThrottle

    monkeypatch.setattr(SimpleRateThrottle, "allow_request", lambda self, request, view: True)
    monkeypatch.setattr(BaseThrottle, "allow_request", lambda self, request, view: True)
    settings.SYNTHETIC_TEST_DATA_ENABLED = False
    settings.SYNTHETIC_TEST_SUBJECT_IDS = []
    GoalOption.objects.create(key="event", label="Собраться к событию")
    category = ServiceCategory.objects.create(name="Макияж", slug="макияж")
    ServiceCategory.objects.create(name="Дизайн ногтей", slug="дизайн-ногтей")
    salon = Tenant.objects.create(slug="synthetic-sweep-demo", name="Демо-салон", is_demo=True)
    persona = User.objects.create_user(username="synthetic-sweep-persona", password="x", is_test_persona=True)
    plain = User.objects.create_user(username="synthetic-sweep-plain", password="x")
    spec = load_spec()
    spec.update(salon_id=str(salon.pk), test_persona_id=str(persona.pk))
    report = seed(spec)

    canons = list(ServiceTemplate.objects.filter(synthetic=True))
    offer = SalonService.objects.get(synthetic=True)
    edge = SpecialistService.objects.get(salon_service=offer)
    capabilities = list(ProcedureCapability.objects.filter(synthetic=True))
    links = list(CapabilityGoalLink.objects.filter(synthetic=True))
    markers = {
        "synthetic_event_", "(тест)", "Причёска к событию", "Макияж к событию", "Ухоженные руки к событию",
        *(str(row.pk) for row in (*canons, offer, edge, *capabilities, *links)),
    }
    values = {
        "specialist_id": [report.master_id],
        "salon_service_id": [str(offer.pk)],
        "tenant_id": [str(salon.pk)],
        "slug": [salon.slug, category.slug],
        "user_id": [str(persona.pk)],
        "ayla_user_id": [str(persona.pk)],
        "pk": [
            report.master_id, str(offer.pk), str(edge.pk), str(salon.pk), str(category.pk),
            *(str(row.pk) for row in (*canons, *capabilities)),
        ],
    }
    query = {
        "tenant": str(salon.pk), "goal_key": "event", "template_id": str(canons[0].pk),
        "salon_service_id": str(offer.pk), "specialist": report.master_id, "specialist_id": report.master_id,
        "category": str(category.pk), "q": "событию", "search": "событию",
    }
    return {
        "markers": markers, "values": values, "query": query, "persona": persona, "plain": plain,
        "master_id": report.master_id,
    }


def _paths(route: str, values: dict) -> list[str] | None:
    """Пути маршрута со всеми подстановками; ``None`` — у набора нет значения для параметра."""
    names = re.findall(r"<(?:\w+:)?(\w+)>", route) + re.findall(r"\(\?P<(\w+)>[^)]*\)", route)
    if any(name not in values for name in names):
        return None
    paths = [route]
    for name in names:
        pattern = re.compile(r"<(?:\w+:)?%s>|\(\?P<%s>[^)]*\)" % (name, name))
        paths = [pattern.sub(value, path, count=1) for path in paths for value in values[name]]
    return ["/" + path.replace("^", "").replace("$", "").replace("\\", "") for path in paths]


def _callers(seeded) -> dict:
    """Вызывающие так, как они приходят на самом деле: приложение клиента и бот.

    Клиентское приложение называет себя заголовком ``X-App-Type: client``; бот
    ходит сервисным токеном и называет человека заголовком
    ``X-External-User-Id`` (здесь — обычный внешний пользователь, не персона).
    """
    anonymous = APIClient()
    anonymous.defaults["HTTP_X_APP_TYPE"] = "client"
    plain = APIClient()
    plain.force_authenticate(seeded["plain"])
    plain.defaults["HTTP_X_APP_TYPE"] = "client"
    persona = APIClient()
    persona.force_authenticate(seeded["persona"])
    persona.defaults["HTTP_X_APP_TYPE"] = "client"
    internal = APIClient()
    internal.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    bot = APIClient()
    bot.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    bot.defaults["HTTP_X_EXTERNAL_USER_ID"] = "bot:max:990000001"
    return {
        "аноним": anonymous, "обычный клиент": plain, "тестовая персона": persona,
        "сервисный токен": internal, "бот от имени человека": bot,
    }


def test_no_reading_endpoint_returns_seeded_synthetic_rows_without_a_grant(seeded) -> None:
    routes = _routes()
    callers = _callers(seeded)
    leaks: list[str] = []
    answered: set[str] = set()
    refused_only: set[str] = set()
    no_values: list[str] = []
    master_seen_by: set[str] = set()

    for route in routes:
        paths = _paths(route, seeded["values"])
        if paths is None:
            no_values.append(route)
            continue
        ok = False
        for path in paths:
            for who, client in callers.items():
                for params in ({}, seeded["query"]):
                    try:
                        response = client.get(path, params)
                    except Exception as error:  # noqa: BLE001 — обход не должен падать на чужой ручке
                        leaks.append(f"{route} [{who}] упала: {type(error).__name__}: {error}"[:300])
                        continue
                    if response.status_code != 200:
                        continue
                    ok = True
                    body = b"".join(response.streaming_content) if getattr(response, "streaming", False) else (
                        response.content
                    )
                    text = body.decode("utf-8", errors="replace")
                    hit = sorted(marker for marker in seeded["markers"] if marker in text)
                    if hit:
                        leaks.append(f"{route} [{who}{' +параметры' if params else ''}] → {hit[:4]}")
                    if seeded["master_id"] in text:
                        master_seen_by.add(who)
        (answered if ok else refused_only).add(route)

    print(
        f"\nОБХОД: маршрутов {len(routes)}; ответили 200 хотя бы одному вызывающему — {len(answered)}; "
        f"всем отказали — {len(refused_only)}; нет значения для параметра — {len(no_values)}; "
        f"тест-мастера видят: {sorted(master_seen_by) or 'никто'}"
    )
    print("НЕ ИСПОЛНЕНО (всем отказали):", *sorted(refused_only), sep="\n  ")
    print("НЕТ ЗНАЧЕНИЯ:", *sorted(no_values), sep="\n  ")
    print("УТЕЧКИ:", *(leaks or ["нет"]), sep="\n  ")

    # Положительный контроль: обход вообще что-то исполнил — иначе «нет утечек» ничего бы не значило.
    assert len(answered) >= 20, f"ответили только {len(answered)} маршрутов — обход ничего не проверил"
    assert leaks == []


def test_the_sweep_would_notice_a_leak(seeded) -> None:
    """Контроль самого обхода: ручка знания под действующим разрешением синтетику ОТДАЁТ, и обход её находит."""
    from services import capabilities
    from services.synthetic import grant_for

    assert list(capabilities.capability_keys_helping_goal("event")) == []

    from django.test import override_settings

    with override_settings(
        SYNTHETIC_TEST_DATA_ENABLED=True, SYNTHETIC_TEST_SUBJECT_IDS=[str(seeded["persona"].pk)],
    ):
        keys = capabilities.capability_keys_helping_goal("event", include_synthetic=grant_for(seeded["persona"]))

    assert any(marker in " ".join(keys) for marker in seeded["markers"])


# ─── ручки на POST: подбор, сборка плана, подписи ────────────────────────────
#
# GET-обход выше нашёл три пути мимо общих читателей (оба зеркала бота и
# выборку поиска). Довод «подбор закрыт фильтром, значит и POST закрыт» верен
# ровно настолько, насколько POST-ручки идут через подбор, — поэтому они
# вызываются здесь исполнением. Тела запросов — рабочие, от владельцев ручек
# (помощники их же узлов), а не подобранные: отказ ручки не должен сойти за
# «не отдаёт». У каждой ручки — положительный контроль на настоящей строке.

RESOLVE_URL = "/api/v1/internal/recommendation/resolve/"
DECISION_URL = "/api/v1/internal/me/plan/decision/"
LABELS_URL = "/api/v1/internal/me/plan/capability-labels/"
SYNTHETIC_KEYS = ["synthetic_event_hair", "synthetic_event_makeup", "synthetic_event_hands"]


def _bot(external_user_id: str) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return client


@pytest.fixture
def bot_users(seeded, settings):
    """Два внешних пользователя бота: обычный и ТЕСТОВАЯ ПЕРСОНА — она видит демо-салон и опаснее всех."""
    from goals.models import ClientGoal

    settings.PLAN_ENGINE_ENABLED = True
    users = {
        "бот: обычный человек": User.objects.create_user(
            username="bot:sweep-plain", password="x", role="client", phone="+79995550001", is_proxy=True,
        ),
        "бот: тестовая персона": User.objects.create_user(
            username="bot:sweep-persona", password="x", role="client", phone="+79995550002", is_proxy=True,
            is_test_persona=True,
        ),
    }
    for user in users.values():
        ClientGoal.objects.create(client=user, goal_key="event", source_channel="bot")
    return users


def _resolve_body(need: dict) -> dict:
    return {
        "request_id": "sweep-1", "surface": "MINIAPP_HOME", "scope": {"mode": "MARKETPLACE"}, "need": need,
        "safety_state": "NORMAL", "tie_break_seed": "s", "k": 20,
    }


@pytest.mark.parametrize("need", [
    {"origin": "MEMORY"},
    {"origin": "USER_EXPLICIT", "raw_text": "укладка"},
    {"origin": "USER_EXPLICIT", "raw_text": "событию"},
])
def test_the_resolver_does_not_return_the_seeded_offer_to_anyone(seeded, bot_users, need) -> None:
    """В ответе подбора нет названий — только идентификаторы; ищутся они, в том числе среди исключённых."""
    from recommendation.tests.test_legal_gate_cat10_ext_2843 import _does, _master, _offer

    salon = Tenant.objects.get(slug="synthetic-sweep-demo")
    curator = User.objects.create_user(username="sweep-curator", password="x")
    resident = _master(salon, "77")
    _does(resident, _offer(
        salon, ServiceCategory.objects.get(slug="макияж"), curator, name="Укладка к событию настоящая",
    ))
    test_master_user = str(SalonService.objects.get(synthetic=True).specialist_services.get().specialist.user_id)
    # Идентификаторы услуги, ребра, канонов, способностей и связей. Самого тест-мастера среди
    # запрещённого нет: он обычный мастер демо-салона, пометки у мастеров нет, и «мастер без
    # услуг» в демо принят. Но появиться он вправе только среди исключённых и только с общей
    # причиной «нет действующих предложений» — не с причиной, говорящей о самой услуге.
    forbidden = {marker for marker in seeded["markers"] if len(marker) == 36}

    seen_resident = set()
    for who, user in bot_users.items():
        response = _bot(user.username).post(RESOLVE_URL, _resolve_body(need), format="json")
        assert response.status_code == 200, (who, response.content[:300])
        text = response.content.decode("utf-8")
        assert not sorted(marker for marker in forbidden if marker in text), (who, need)
        data = response.json()["data"]
        assert test_master_user not in {row["candidate"]["id"] for row in data["ordered"]}, (who, need)
        about_him = [row["reason_code"] for row in data["excluded"] if row["candidate"]["id"] == test_master_user]
        assert about_him in ([], ["ELIG_EXCLUDED_INACTIVE"]), (who, need, about_him)
        if str(resident.user_id) in text:
            seen_resident.add(who)

    # Положительный контроль: настоящего мастера того же демо-салона тестовая
    # персона получает — значит, подбор исполнился и демо-салон ей виден.
    assert "бот: тестовая персона" in seen_resident
    assert "бот: обычный человек" not in seen_resident


def test_plan_composition_and_labels_do_not_read_the_seeded_knowledge(seeded, bot_users) -> None:
    from wellness.tests.test_plan_compose_2871 import _body

    for who, user in bot_users.items():
        decision = _bot(user.username).post(DECISION_URL, {**_body(), **ATTESTATION}, format="json")
        assert decision.status_code == 200, (who, decision.content[:300])
        assert decision.json()["data"]["outcome"] == "NO_CURATED_DECOMPOSITION", who
        assert not [m for m in seeded["markers"] if m in decision.content.decode("utf-8")], who

        labels = _bot(user.username).post(LABELS_URL, {"keys": SYNTHETIC_KEYS}, format="json")
        assert labels.status_code == 200, (who, labels.content[:300])
        answer = labels.json()["data"]["labels"]
        assert {key: (row["state"], row["label"]) for key, row in answer.items()} == {
            key: ("unknown", None) for key in SYNTHETIC_KEYS
        }, who


def test_plan_composition_control_real_knowledge_gives_a_plan_without_synthetic_steps(seeded, bot_users) -> None:
    """Положительный контроль сборки: с настоящим знанием план есть, и синтетических шагов в нём нет."""
    from wellness.tests.test_plan_compose_2871 import _body, _capability

    goal_option = GoalOption.objects.get(key="event")
    curator = User.objects.create_user(username="sweep-knowledge-curator", password="x")
    category = ServiceCategory.objects.get(slug="макияж")
    for index, key in enumerate(["real_event_first", "real_event_second"]):
        canon = ServiceTemplate.objects.create(category=category, name=f"Настоящий канон {index}", name_short="Наст")
        _capability(canon, key, curator, goal_option)

    for who, user in bot_users.items():
        data = _bot(user.username).post(DECISION_URL, {**_body(), **ATTESTATION}, format="json").json()["data"]
        assert data["outcome"] == "PLAN", who
        refs = sorted(step["capability_ref"] for step in data["decision"]["steps"])
        assert refs == ["real_event_first", "real_event_second"], who


# ─── слоты: путь, который закрывать НЕЛЬЗЯ ───────────────────────────────────


def test_slots_of_the_test_master_come_for_the_seeded_service(seeded) -> None:
    """Слоты — время, а не знание и не предложение; у ручки нет личности, фильтр закрыл бы её наглухо.

    Бот в ветке плана берёт идентификаторы мастера и услуги из ответа
    кандидатов (он уже прошёл серверное разрешение) и идёт за временем сюда.
    Узел держит, что путь открыт, и что в ответе нет описания услуги.
    Сама ручка разрешения не проверяет: идентификатор синтетической услуги
    обычному клиенту взять неоткуда — это и держит обход выше.
    """
    from datetime import timedelta

    from django.utils import timezone

    offer = SalonService.objects.get(synthetic=True)
    day = (timezone.localdate() + timedelta(days=2)).isoformat()
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"

    response = client.get(
        f"/api/v1/internal/specialists/{seeded['master_id']}/slots/", {"service_id": str(offer.pk), "date": day},
    )

    assert response.status_code == 200, response.content[:400]
    text = response.content.decode("utf-8")
    payload = response.json()
    slots = payload.get("slots") if isinstance(payload, dict) else None
    if slots is None and isinstance(payload, dict):
        slots = (payload.get("data") or {}).get("slots")
    assert slots, f"свободных окон нет: {text[:300]}"
    assert "(тест)" not in text and "Причёска к событию" not in text and "synthetic_event_" not in text


# ─── кандидаты услуги для шага: худший путь утечки ───────────────────────────


@pytest.mark.parametrize("key", ["synthetic_event_hair", "synthetic_event_makeup"])
def test_step_candidates_do_not_offer_the_seeded_service_without_a_grant(seeded, bot_users, key, settings) -> None:
    """Шаг сохранённого плана несёт синтетическую способность; без разрешения услугу под него не предлагают.

    Утечка здесь была бы хуже всего: синтетическое предложение обычному
    клиенту с прямой дорогой к записи. Вызывающие — обычный человек и
    тестовая персона (она видит демо-салон), настройки стенда выключены.

    DRF-2871: рубежей два. Без разрешения план на синтетической способности
    не сохранить вовсе — у обычного человека такого плана быть не может. У
    тестовой персоны он остаётся с прогона: сохранён под разрешением,
    разрешение снято — и кандидаты обязаны молчать.
    """
    from goals.models import ClientGoal
    from wellness.plan_engine import CapabilityNotConfirmed
    from wellness.tests.test_plan_engine_steps_2868 import SAFETY, _save, _step
    from wellness.tests.test_plan_step_candidates_2868 import CANDIDATES_URL

    offer = SalonService.objects.get(synthetic=True)
    edge = offer.specialist_services.get()
    forbidden = {str(offer.pk), str(edge.pk), str(edge.specialist_id), str(edge.specialist.user_id), "(тест)"}

    for who, user in bot_users.items():
        goal = ClientGoal.objects.get(client=user)
        steps = [_step("s1", capability_ref=key, outcome_ref="event")]
        with pytest.raises(CapabilityNotConfirmed):
            _save(goal, steps=steps)
        if who != "бот: тестовая персона":
            continue
        settings.SYNTHETIC_TEST_DATA_ENABLED = True
        settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(user.pk)]
        plan = _save(goal, steps=steps)
        settings.SYNTHETIC_TEST_DATA_ENABLED = False
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []

        response = _bot(user.username).post(
            CANDIDATES_URL, {"plan_id": str(plan.id), "step_id": "s1", **SAFETY, **ATTESTATION}, format="json",
        )

        assert response.status_code == 200, (who, response.content[:400])
        text = response.content.decode("utf-8")
        assert not sorted(marker for marker in forbidden if marker in text), (who, key, text[:400])


@pytest.mark.parametrize("who", ["бот: обычный человек", "бот: тестовая персона"])
@pytest.mark.parametrize("marked", [False, True])
def test_step_candidates_stay_silent_for_a_plan_that_got_past_saving(
    seeded, bot_users, monkeypatch, who, marked,
) -> None:
    """Защита в глубину: рубеж сохранения обойдён — кандидаты всё равно молчат.

    План с синтетическим шагом здесь заведён МИМО проверки знания при
    сохранении (она подменена), как если бы он попал в базу другим путём:
    непомеченный — и помеченный, но у человека без разрешения. Ручка
    кандидатов на рубеж сохранения не опирается: разрешения нет — услуги нет.
    """
    from goals.models import ClientGoal
    from wellness import plan_engine
    from wellness.tests.test_plan_engine_steps_2868 import SAFETY, _save, _step
    from wellness.tests.test_plan_step_candidates_2868 import CANDIDATES_URL

    offer = SalonService.objects.get(synthetic=True)
    edge = offer.specialist_services.get()
    forbidden = {str(offer.pk), str(edge.pk), str(edge.specialist_id), str(edge.specialist.user_id), "(тест)"}
    user = bot_users[who]
    monkeypatch.setattr(plan_engine, "_synthetic_or_refuse", lambda *args, **kwargs: marked)
    plan = _save(
        ClientGoal.objects.get(client=user),
        steps=[_step("s1", capability_ref="synthetic_event_hair", outcome_ref="event")],
    )
    monkeypatch.undo()
    assert plan.synthetic is marked  # контроль: план действительно заведён и помечен так, как задумано

    response = _bot(user.username).post(
        CANDIDATES_URL, {"plan_id": str(plan.id), "step_id": "s1", **SAFETY, **ATTESTATION}, format="json",
    )

    assert response.status_code == 200, (who, marked, response.content[:400])
    text = response.content.decode("utf-8")
    assert not sorted(marker for marker in forbidden if marker in text), (who, marked, text[:400])
    assert response.json()["data"]["candidates"] == [], text[:400]


def test_step_candidates_control_a_real_capability_gives_a_real_candidate(seeded, bot_users) -> None:
    """Положительный контроль: та же ручка тем же вызывающим отдаёт настоящего кандидата — она исполнилась."""
    from goals.models import ClientGoal
    from wellness.tests.test_plan_engine_steps_2868 import SAFETY, _save, _step
    from wellness.tests.test_plan_step_candidates_2868 import CANDIDATES_URL, KEY, _can, _ready_offer

    salon = Tenant.objects.create(slug="synthetic-sweep-real", name="Настоящий салон")
    curator = User.objects.create_user(username="sweep-candidates-curator", password="x")
    _, real_offer = _ready_offer(
        salon, ServiceCategory.objects.get(slug="макияж"), curator, name="Настоящая услуга шага", suffix="88",
    )
    # DRF-2871: план сохраняется только на способности, подтверждённо помогающей цели.
    from wellness.tests.knowledge import confirm_capability_for_goal

    confirm_capability_for_goal(KEY, goal_key="event")
    _can([real_offer.template], curator)
    user = bot_users["бот: обычный человек"]
    plan = _save(ClientGoal.objects.get(client=user), steps=[_step("s1", capability_ref=KEY, outcome_ref="event")])

    response = _bot(user.username).post(
        CANDIDATES_URL, {"plan_id": str(plan.id), "step_id": "s1", **SAFETY, **ATTESTATION}, format="json",
    )

    assert response.status_code == 200, response.content[:400]
    assert str(real_offer.pk) in response.content.decode("utf-8"), response.content[:600]
