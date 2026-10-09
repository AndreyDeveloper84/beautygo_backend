"""Обход всех читающих ручек каталога: засеянная синтетика не видна никому без разрешения.

Условие перед засевом на стенд: «фильтр в подборе слит» не равно «синтетика
не видна ни в одной клиентской ручке». Перечень ручек здесь не составлен
руками — он снят с маршрутов приложения, а ответ каждой ручки ищется на
следы засеянного набора. Что вернулось, то и есть перечень.

Узел засевает набор штатной командой (``services.synthetic_fixture``), затем
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
from services.synthetic_fixture import load_spec, seed
from tenants.models import Tenant
from users.models import User

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
