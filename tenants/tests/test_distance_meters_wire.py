"""`distance_meters: integer | null` — каноническое поле провода (OD-PILOT-9, решение 12.09 02:43).

Три вещи:

* **производное, не второй расчёт** — метры получаются из километров одной
  функцией; ``null`` ровно там, где ``distance_km`` ``null`` (DISTANCE_UNKNOWN);
* **везде, где есть километры, есть и метры** — структурный сторож по AST:
  каждый модуль вне тестов, который кладёт ключ ``distance_km`` в ответ
  (поле сериализатора или ключ словаря), кладёт и ``distance_meters``. Дубль
  ``distance_km`` живёт один релиз; когда его снимут, сторож перевернуть —
  «километров на проводе нет»;
* **поведенческий** — карточка мастера и поиск с `lat/lon` отдают оба поля
  согласованно; без координаты клиента — оба ``null``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from tenants.distance import distance_meters
from tenants.tests.places import place_specialist_at
from users.models import SpecialistProfile, User

ROOT = Path(__file__).resolve().parents[2]

#: Внутренние DTO и замеры, где distance_km — не провод, а поле структуры.
INTERNAL_NOT_WIRE = {
    "tenants/distance.py",
    "ai/application/services/recommendation_engine.py",
    "ai/application/services/specialist_context_builder.py",
    "recommendation/_types.py",
    "recommendation/_stages.py",
    "ai/concierge_factory.py",
    "ai/management/commands/check_distance_readiness.py",
}


@pytest.mark.parametrize("km, meters", [
    (None, None), (0.0, 0), (0.0004, 0), (0.0005, 0), (0.0006, 1),
    (1.8, 1800), (1.23456, 1235), (25.0, 25000),
])
def test_meters_are_derived_from_km_and_null_stays_null(km, meters):
    assert distance_meters(km) == meters
    assert distance_meters(km) is None or isinstance(distance_meters(km), int)


def _emits(tree: ast.AST, key: str) -> bool:
    """Модуль кладёт ``key`` на провод: строка в списке ``fields`` / ключ словаря / имя поля-атрибута."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and n.value == key:
            return True
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == key for t in n.targets):
            return True
    return False


def test_every_emitter_of_km_also_emits_meters():
    """Ни одного ответа с километрами без метров. Список эмиттеров — положительный контроль."""
    km_only, both = [], []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or "/migrations/" in rel or any(
            p in (".venv", "venv", "node_modules", "docs") for p in path.parts
        ):
            continue
        if rel in INTERNAL_NOT_WIRE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_km, has_m = _emits(tree, "distance_km"), _emits(tree, "distance_meters")
        if has_km and not has_m:
            km_only.append(rel)
        if has_km and has_m:
            both.append(rel)
    expected = {"users/specialists_api.py", "search/views.py", "users/home_api.py", "ai/tools_handlers.py"}
    assert expected <= set(both), both
    assert not km_only, "километры без метров: " + ", ".join(km_only)


@pytest.fixture
def placed_master(db):
    u = User.objects.create_user(username="dm-1", password="x", role="specialist", phone="+79990001800")
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "Метры"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.save()
    place_specialist_at(p, "53.195878", "45.018316")
    return p


@pytest.mark.django_db
def test_specialist_card_carries_both_fields_consistently(placed_master):
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "client"
    viewer = User.objects.create_user(username="dm-viewer", password="x", role="client", phone="+79990001801")
    client.force_authenticate(user=viewer)
    r = client.get(f"/api/v1/specialists/{placed_master.id}/", {"lat": "53.2", "lon": "45.0"})
    assert r.status_code == 200, r.content[:200]
    km, m = r.data["distance_km"], r.data["distance_meters"]
    assert km is not None and isinstance(m, int) and m == int(round(km * 1000))
    r = client.get(f"/api/v1/specialists/{placed_master.id}/")
    assert r.data["distance_km"] is None and r.data["distance_meters"] is None
