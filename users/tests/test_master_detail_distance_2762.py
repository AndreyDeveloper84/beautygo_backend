"""Расстояние в карточке мастера: ``GET /api/v1/internal/specialists/{id}/?lat&lon`` (DRF-2762).

Лист просил добавить в детальную ручку мастера необязательные ``lat``/``lon``.
Замер до кода показал: ручка их уже принимает — ``InternalSpecialistViewSet``
наследует ``SpecialistDetailSerializer`` с ``DistanceMixin`` — и считает
расстояние до **места предложения** мастера (``SpecialistProfile.works_at`` →
``ServiceLocation.latitude/longitude``, решение §9), а не до полей тенанта.
Узла на внутренней ручке не было ни одного; этот файл — он, и он же живой
контракт для карточки в боте (DRF-2755).

Настоящий дефект нашёлся в том же замере: ``?lat=nan`` разбирался как число и
ронял ответ на ``int(round(nan))`` — 500 на карточке, обоих списках и поиске у
любого мастера с местом; ``?radius`` с NaN ронял список отдельно. Широта 999
давала 16 805 км вместо «неизвестно». Починено в ``tenants/distance.py`` —
единственном месте, где расстояние считается.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from tenants.models import Tenant
from tenants.tests.places import place_specialist_at
from users.models import SpecialistProfile, User

TOKEN = "test-ayla-internal-token-2762"

#: Место мастера и точка клиента. Расстояние между ними — литералом ниже:
#: посчитай его узел той же формулой, что код, он согласился бы с любой.
PLACE = ("53.195878", "45.018316")
CLIENT = {"lat": "53.200000", "lon": "45.000000"}
METERS = 1303

#: Ввод, который не точка на глобусе. Ни один не должен дать ни 5xx, ни числа.
NOT_A_POINT = [
    ("only_lat", {"lat": "53.2"}),
    ("only_lon", {"lon": "45.0"}),
    ("empty", {"lat": "", "lon": ""}),
    ("text", {"lat": "abc", "lon": "45.0"}),
    ("comma", {"lat": "53,2", "lon": "45,0"}),
    ("nan_lat", {"lat": "nan", "lon": "45.0"}),
    ("nan_lon", {"lat": "53.2", "lon": "NaN"}),
    ("nan_both", {"lat": "nan", "lon": "nan"}),
    ("inf", {"lat": "inf", "lon": "45.0"}),
    ("overflow", {"lat": "1e400", "lon": "45.0"}),
    ("lat_out_of_range", {"lat": "999", "lon": "45.0"}),
    ("lon_out_of_range", {"lat": "53.2", "lon": "200"}),
]


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="dist-2762", name="Расстояние 2762")


def _master(tenant: Tenant, n: int) -> SpecialistProfile:
    u = User.objects.create_user(
        username=f"dist2762-{n}", password="x", role="specialist", phone=f"+7999276210{n}",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = f"Мастер {n}"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.save()
    return p


@pytest.fixture
def placed(tenant):
    """Мастер в подтверждённом геокодированном месте."""
    p = _master(tenant, 1)
    place_specialist_at(p, *PLACE)
    return p


@pytest.fixture
def unplaced(tenant):
    """Мастер того же салона без места: расстояние до него неизвестно."""
    return _master(tenant, 2)


@pytest.fixture
def api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    # 5xx должен прийти ответом, а не исключением теста: узлы ниже утверждают
    # именно код ответа.
    c.raise_request_exception = False
    return c


def _detail(api: APIClient, master: SpecialistProfile, params: dict | None = None):
    return api.get(f"/api/v1/internal/specialists/{master.id}/", params or {})


@pytest.mark.django_db
def test_distance_is_returned_in_whole_meters_to_the_masters_place(api, placed):
    r = _detail(api, placed, CLIENT)
    assert r.status_code == 200, r.content[:200]
    body = r.json()
    assert body["distance_meters"] == METERS
    assert type(body["distance_meters"]) is int
    # Дубль на один релиз (OD-PILOT-9): километры дробью, метры производны.
    assert body["distance_km"] == pytest.approx(1.3033, abs=1e-4)
    assert body["distance_meters"] == int(round(body["distance_km"] * 1000))


@pytest.mark.django_db
def test_without_coordinates_the_answer_keeps_its_shape_and_says_unknown(api, placed):
    with_point = _detail(api, placed, CLIENT).json()
    without = _detail(api, placed).json()
    assert with_point["distance_meters"] == METERS
    # Обратная совместимость: набор ключей один и тот же, отличаются только
    # два поля расстояния. Читатель, который координат не шлёт, видит то же,
    # что видел.
    assert set(without) == set(with_point)
    assert without["distance_meters"] is None and without["distance_km"] is None
    changed = {k for k in without if without[k] != with_point[k]}
    assert changed == {"distance_km", "distance_meters"}


@pytest.mark.django_db
def test_a_master_without_a_place_is_unknown_not_zero(api, placed, unplaced):
    assert _detail(api, placed, CLIENT).json()["distance_meters"] == METERS
    body = _detail(api, unplaced, CLIENT).json()
    assert body["distance_meters"] is None and body["distance_km"] is None


@pytest.mark.django_db
def test_zero_is_a_real_distance_and_is_not_confused_with_unknown(api, placed):
    body = _detail(api, placed, {"lat": PLACE[0], "lon": PLACE[1]}).json()
    assert body["distance_meters"] == 0 and body["distance_meters"] is not None


@pytest.mark.django_db
@pytest.mark.parametrize("params", [p for _, p in NOT_A_POINT], ids=[n for n, _ in NOT_A_POINT])
def test_input_that_is_not_a_point_gives_unknown_and_never_a_server_error(api, placed, params):
    assert _detail(api, placed, CLIENT).json()["distance_meters"] == METERS
    r = _detail(api, placed, params)
    assert r.status_code == 200, r.content[:200]
    body = r.json()
    assert body["distance_meters"] is None and body["distance_km"] is None


@pytest.mark.django_db
def test_not_a_number_does_not_break_the_neighbouring_readers(api, placed, tenant):
    """Тот же ``DistanceMixin`` и та же формула стоят под списками и поиском."""
    viewer = User.objects.create_user(username="dist2762-v", password="x", role="client", phone="+79992762199")
    public = APIClient()
    public.defaults["HTTP_X_APP_TYPE"] = "client"
    public.force_authenticate(user=viewer)
    public.raise_request_exception = False
    nan = {"lat": "nan", "lon": "45.0"}
    scoped = {"tenant": str(tenant.id)}

    # Положительный контроль: с настоящей точкой список отдаёт этого мастера
    # с расстоянием — значит, запросы ниже доходят до расчёта.
    ok = api.get("/api/v1/internal/specialists/", {**CLIENT, **scoped})
    rows = ok.json()["results"] if isinstance(ok.json(), dict) else ok.json()
    assert [row["distance_meters"] for row in rows if row["id"] == str(placed.id)] == [METERS]

    answers = {
        "internal_list": api.get("/api/v1/internal/specialists/", {**nan, **scoped}),
        "internal_list_radius": api.get("/api/v1/internal/specialists/", {**nan, **scoped, "radius": "5"}),
        "radius_nan": api.get("/api/v1/internal/specialists/", {**CLIENT, **scoped, "radius": "nan"}),
        "radius_inf": api.get("/api/v1/internal/specialists/", {**CLIENT, **scoped, "radius": "inf"}),
        "public_detail": public.get(f"/api/v1/specialists/{placed.id}/", nan),
        "public_list": public.get("/api/v1/specialists/", nan),
        "search": public.get("/api/v1/search/", {**nan, "q": "Мастер"}),
    }
    assert {name: r.status_code for name, r in answers.items()} == dict.fromkeys(answers, 200)
