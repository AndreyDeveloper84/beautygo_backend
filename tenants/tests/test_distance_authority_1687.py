"""Расстояние считается до места предложения и нигде больше (§9 / L5, DRF-1687).

Три сторожа и одна «обратная» проверка:

* **структурный** — во всём репозитории (кроме тестов, миграций, моделей,
  админки и двух замерных команд, названных поимённо) ни один вызов
  ``*haversine*`` не получает аргументом ``location_lat`` / ``location_lng``,
  и ни один ``filter(...)`` / ``Q(...)`` не фильтрует по
  ``location_lat__*`` / ``location_lng__*``. Формула — одна,
  ``tenants.distance.haversine_km``; все ``_haversine`` вне модуля либо сняты,
  либо делегируют ей;
* **положительный контроль** на структурный — сканер обязан видеть сами
  вызовы ``distance_km_to`` в четырёх местах L5; иначе «ничего не нашёл»
  значило бы «ничего не искал»;
* **поведенческий** — `participating_place_q` (для ORM) и
  ``participates_in_distance`` (для объекта) согласны на всех сочетаниях
  статусов: строка, которую пропускает одно, пропускает другое;
* **DISTANCE_UNKNOWN** — ``distance_km_to`` даёт ``None`` без места, без
  подтверждения, без геокода и без координаты клиента — и число при всём
  сразу (положительная сторона).
"""
from __future__ import annotations

import ast
from decimal import Decimal as D
from itertools import product
from pathlib import Path

import pytest

from tenants.distance import bbox_q, distance_km_to, haversine_km, participating_place_q
from tenants.models import GeocodeStatus, LocationStatus, ServiceLocation, Tenant
from users.models import SpecialistProfile, User

ROOT = Path(__file__).resolve().parents[2]
MASTER_COORDS = {"location_lat", "location_lng"}

#: Модули, которым ЧИТАТЬ координаты профиля мастера ещё можно — и почему.
#: Список закрыт: расширение — осознанный шаг с причиной, не молчаливый импорт.
ALLOWED_READERS = {
    "users/models.py": "определение полей; снимаются в L8, когда читателей 0",
    "users/admin.py": "форма мастера показывает старые поля до L8, подсказка говорит «не заполнять»",
    "core/management/commands/surface_state.py": "замер состояния: считает, сколько старых координат осталось",
    "ai/management/commands/check_distance_readiness.py": "замер готовности; переключается на ServiceLocation в L6",
    "services/serializers.py": "публичный сериализатор мастера — L6",
    "users/serializers.py": "сериализаторы профиля — L6",
    "search/views.py": "поле в fields сериализатора поиска — L6 (расстояние там УЖЕ по месту)",
    "users/specialists_api.py": "поле в fields сериализаторов — L6 (расстояние и bbox УЖЕ по месту)",
    "ai/views.py": "одноимённое поле контекста чата — координата КЛИЕНТА (§8), не мастера",
    "ai/application/services/chat_service.py": "то же одноимённое поле контекста чата — координата клиента",
    "users/deletion_executor.py": "стирание, не чтение: исполнитель удаления аккаунта (§7 D3) обнуляет "
    "координаты профиля и по перечитанной строке проверяет, что их нет; расстояние не считает, не фильтрует",
}

#: Фильтровать ORM по старым координатам вправе только замер состояния.
#: Список читателей выше это НЕ разрешает: там — поля сериализаторов.
FILTER_ALLOWED = {"core/management/commands/surface_state.py"}

LOCAL_HAVERSINE_ALLOWED = {
    "tenants/distance.py",                                   # единственная формула
    "ai/application/services/recommendation_engine.py",     # делегирует
    "ai/management/commands/check_distance_readiness.py",   # своя обёртка над формулой движка
}


def _py_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or "/migrations/" in rel or any(
            p in (".venv", "venv", "node_modules", "docs") for p in path.parts
        ):
            continue
        yield rel, path


def _mentions_in_code(tree: ast.AST) -> bool:
    """Имя поля встречается как идентификатор, атрибут, ключ-строка или kwarg."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in MASTER_COORDS:
            return True
        if isinstance(n, ast.Name) and n.id in MASTER_COORDS:
            return True
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and (
            n.value in MASTER_COORDS
            or any(n.value.endswith("." + c) for c in MASTER_COORDS)  # DRF source='specialist.location_lat'
        ):
            return True
        if isinstance(n, ast.keyword) and n.arg and n.arg.split("__")[0] in MASTER_COORDS:
            return True
        if isinstance(n, ast.arg) and n.arg in MASTER_COORDS:
            return True
    return False


def _attr_names(node) -> set[str]:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def test_no_distance_is_computed_or_filtered_on_the_masters_own_coordinates():
    offenders, distance_call_sites = [], set()
    for rel, path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = ast.unparse(node.func)
            if callee.endswith("distance_km_to"):
                distance_call_sites.add(rel)
            if "haversine" in callee:
                if MASTER_COORDS & set().union(*(_attr_names(a) for a in node.args)):
                    offenders.append(f"{rel}: haversine по координатам профиля мастера")
            if callee.endswith((".filter", ".exclude", "Q")):
                for kw in node.keywords:
                    if kw.arg and kw.arg.split("__")[0] in MASTER_COORDS and rel not in FILTER_ALLOWED:
                        offenders.append(f"{rel}: фильтр по {kw.arg}")
    # положительный контроль: четыре места L5 действительно зовут единый помощник
    assert {"ai/application/services/recommendation_engine.py", "search/views.py",
            "users/specialists_api.py"} <= distance_call_sites, distance_call_sites
    assert not offenders, "\n".join(offenders)


def test_readers_of_master_coordinates_are_a_closed_named_set():
    """Каждый читатель старых полей назван с причиной; появление нового — красный."""
    readers = set()
    for rel, path in _py_files():
        # По коду, не по тексту: докстринг, упоминающий поле, — не читатель.
        # (Сторож, читающий комментарий, зеленел бы на подменённом коде.)
        if _mentions_in_code(ast.parse(path.read_text(encoding="utf-8"))):
            readers.add(rel)
    unexpected = readers - set(ALLOWED_READERS)
    assert not unexpected, "новые читатели координат профиля мастера: " + ", ".join(sorted(unexpected))
    # и наоборот: список не должен держать мёртвые записи
    stale = set(ALLOWED_READERS) - readers
    assert not stale, "в ALLOWED_READERS есть модули, которые уже не читают: " + ", ".join(sorted(stale))


def test_local_haversine_definitions_are_only_delegates():
    """Формула одна. Локальные ``_haversine`` либо сняты, либо зовут ``haversine_km``."""
    for rel, path in _py_files():
        if rel in LOCAL_HAVERSINE_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and "haversine" in node.name:
                pytest.fail(f"{rel}: своя формула {node.name} — вторая копия расстояния")


# ---------------------------------------------------------------------------
# Поведение
# ---------------------------------------------------------------------------

PENZA = (D("53.195878"), D("45.018316"))


def _master(db, tenant=None, phone="+79990001700"):
    u = User.objects.create_user(username=f"m{phone[-4:]}", password="x", role="specialist", phone=phone)
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.save()
    return p


@pytest.mark.django_db
@pytest.mark.parametrize("status, geocode, coords", list(product(
    [LocationStatus.CONFIRMED, LocationStatus.REVIEW_REQUIRED, LocationStatus.INACTIVE],
    [GeocodeStatus.OK, GeocodeStatus.CONFIRMED, GeocodeStatus.PENDING, GeocodeStatus.AMBIGUOUS],
    ["penza", "none"],
)))
def test_orm_predicate_and_object_property_agree(status, geocode, coords):
    """Копия условия в SQL неизбежна — значит обе стороны стерегутся вместе."""
    tenant = Tenant.objects.create(slug="agree", name="A")
    lat, lng = PENZA if coords == "penza" else (None, None)
    place = ServiceLocation.objects.create(
        tenant=tenant, address="x", status=status, geocode_status=geocode,
        latitude=lat, longitude=lng, geocode_provider="t",
        confirmed_by=None, confirmed_at=None, confirmed_source_ref="",
    ) if status != LocationStatus.CONFIRMED else ServiceLocation.objects.create(
        tenant=tenant, address="x", status=status, geocode_status=geocode,
        latitude=lat, longitude=lng, geocode_provider="t",
        confirmed_by=User.objects.create_user(username="c", password="x", phone="+79990001701"),
        confirmed_at="2026-09-11T00:00:00Z", confirmed_source_ref="§10",
    )
    m = _master(None, tenant)
    m.works_at = place
    m.save(update_fields=["works_at"])

    by_orm = SpecialistProfile.objects.filter(pk=m.pk).filter(participating_place_q()).exists()
    assert by_orm is place.participates_in_distance


@pytest.mark.django_db
def test_distance_unknown_in_every_unknown_case_and_a_number_when_all_known():
    tenant = Tenant.objects.create(slug="dist", name="D")
    op = User.objects.create_user(username="op-d", password="x", phone="+79990001702")
    live = ServiceLocation.objects.create(
        tenant=tenant, address="x", status=LocationStatus.CONFIRMED, geocode_status=GeocodeStatus.OK,
        latitude=PENZA[0], longitude=PENZA[1], geocode_provider="t",
        confirmed_by=op, confirmed_at="2026-09-11T00:00:00Z", confirmed_source_ref="§10",
    )
    unconfirmed = ServiceLocation.objects.create(
        tenant=tenant, address="y", geocode_status=GeocodeStatus.OK,
        latitude=PENZA[0], longitude=PENZA[1], geocode_provider="t",
    )
    m = _master(None, tenant, "+79990001703")

    # нет места — но СТАРЫЕ координаты профиля есть: их читать нельзя
    SpecialistProfile.objects.filter(pk=m.pk).update(location_lat=PENZA[0], location_lng=PENZA[1])
    m.refresh_from_db()
    assert distance_km_to(m, 53.2, 45.0) is None

    m.works_at = unconfirmed
    assert distance_km_to(m, 53.2, 45.0) is None          # не подтверждено
    m.works_at = live
    assert distance_km_to(m, None, 45.0) is None          # нет координаты клиента (§8: не угадываем)
    km = distance_km_to(m, 53.2, 45.0)
    assert km is not None and 0 < km < 2                  # положительная сторона
    assert km == pytest.approx(haversine_km(53.2, 45.0, float(PENZA[0]), float(PENZA[1])))


@pytest.mark.django_db
def test_bbox_excludes_the_unknown_rather_than_treating_it_as_far():
    tenant = Tenant.objects.create(slug="bbox", name="B")
    op = User.objects.create_user(username="op-b", password="x", phone="+79990001704")
    near = ServiceLocation.objects.create(
        tenant=tenant, address="n", status=LocationStatus.CONFIRMED, geocode_status=GeocodeStatus.OK,
        latitude=PENZA[0], longitude=PENZA[1], geocode_provider="t",
        confirmed_by=op, confirmed_at="2026-09-11T00:00:00Z", confirmed_source_ref="§10",
    )
    placed = _master(None, tenant, "+79990001705")
    placed.works_at = near
    placed.save(update_fields=["works_at"])
    unplaced = _master(None, tenant, "+79990001706")
    SpecialistProfile.objects.filter(pk=unplaced.pk).update(location_lat=PENZA[0], location_lng=PENZA[1])

    inside = set(SpecialistProfile.objects.filter(bbox_q(53.2, 45.0, 5)).values_list("pk", flat=True))
    assert inside == {placed.pk}   # старые координаты профиля в радиус не попадают ни при каком радиусе
    assert not SpecialistProfile.objects.filter(bbox_q(53.2, 45.0, 10000)).filter(pk=unplaced.pk).exists()
