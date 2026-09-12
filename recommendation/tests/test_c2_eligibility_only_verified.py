"""Инвариант C2: право на рекомендацию = ТОЛЬКО ``VERIFIED``; всё остальное
видно в каталоге, ищется и бронируется напрямую — но не рекомендуется.

Две половины одного инварианта, и стеречь надо обе:

* **допуск** (``recommendation/_stages.py::_mapping_admission``) — одна ветка,
  без второй и без «измерения»: решение о кандидате не зависит ни от
  политики, ни от того, сколько ``VERIFIED`` в базе (§76: ноль подтверждённых
  не разрешает откат). Поведение этой половины уже стерегут
  ``test_pipeline`` (все статусы, кроме ``VERIFIED``, — мимо) и
  ``test_boundary_guards`` (рычага в настройках нет). Здесь добавлен
  **структурный** сторож на саму функцию: вторую ветку нельзя дописать,
  не покраснев, даже если в тестах для неё не нашлось статуса;
* **видимость** — ``catalog_visible ≠ recommendation_eligible`` (§10.1):
  каталог, поиск и прямая запись **не читают** ``mapping_status``. Иначе
  первый же «ноль VERIFIED» опустошил бы не полку рекомендаций, а весь
  каталог, и человек остался бы перед пустым экраном. Сторож — по коду
  (AST), с положительным контролем: источник резолвера поле читает, и
  сканер это видит.

Плюс поведенческая половина видимости: мастер, чья единственная услуга
``UNMAPPED``, есть в поиске и в карточке.
"""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STAGES = ROOT / "recommendation" / "_stages.py"

#: Где живут каталог, поиск и прямая запись. Ни один из них не вправе
#: читать статус связи: их предмет — «что продаётся», не «что рекомендуем».
VISIBILITY_SURFACES = (
    "search",
    "services/views.py",
    "services/catalog_reads.py",
    "users/specialists_api.py",
    "users/home_api.py",
    "appointments",
)

#: Положительный контроль сканера: единственный законный читатель статуса
#: вне резолвера — источник фактов для него.
RESOLVER_SOURCE = "users/recommendation_source.py"


def _py_files(*roots: str):
    for root in roots:
        base = ROOT / root
        paths = [base] if base.is_file() else base.rglob("*.py")
        for path in paths:
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or "/migrations/" in rel or "/management/" in rel:
                continue
            yield rel, path


def _reads_mapping_status(tree: ast.AST) -> list[str]:
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == "mapping_status":
            hits.append(f"{n.lineno}: атрибут .mapping_status")
        elif isinstance(n, ast.keyword) and n.arg and "mapping_status" in n.arg:
            hits.append(f"{n.lineno}: фильтр {n.arg}")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("mapping_status"):
            hits.append(f"{n.lineno}: строка {n.value!r}")
    return hits


# ---------------------------------------------------------------------------
# Половина 1: допуск — одна ветка, без измерения
# ---------------------------------------------------------------------------

def _admission_fn() -> ast.FunctionDef:
    tree = ast.parse(STAGES.read_text(encoding="utf-8"))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_mapping_admission"]
    assert len(fns) == 1, "функция допуска должна быть ровно одна"
    return fns[0]


def test_admission_has_exactly_one_branch_and_it_is_verified():
    """Единственное условие в функции — ``mapping_status is VERIFIED``.

    Второй ``if`` (по политике, по счётчику, по настройке) — красный, каким
    бы ни было его тело. Сторож на форму, а не на сегодняшний статус: он
    краснеет и на ветке, для которой в тестах ещё нет значения.
    """
    fn = _admission_fn()
    ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    assert len(ifs) == 1, f"в _mapping_admission {len(ifs)} условий, ожидается одно"
    test = ast.unparse(ifs[0].test)
    assert test == "facts.mapping_status is MappingStatus.VERIFIED", test
    # и ветка `else`/`elif` у него не растёт
    assert not ifs[0].orelse


def test_admission_returns_true_exactly_once_and_reads_no_measurement():
    """Один допускающий ``return``; всё остальное — отказ. И ни одного чтения
    ``policy``: допуск не смотрит ни на счётчики, ни на версии политики.
    Иначе «ноль VERIFIED → пустить REVIEW_REQUIRED» вернулось бы как
    «измерение», а не как ветка, — и сторож выше его бы не заметил."""
    fn = _admission_fn()
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    admitting = [
        r for r in returns
        if isinstance(r.value, ast.Tuple) and r.value.elts
        and isinstance(r.value.elts[0], ast.Constant) and r.value.elts[0].value is True
    ]
    assert len(admitting) == 1, [ast.unparse(r) for r in returns]
    read_names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    assert "policy" not in read_names, "допуск читает политику — это второй вход в решение"
    assert not any(
        isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] in {"count", "len", "exists", "filter"}
        for n in ast.walk(fn)
    ), "допуск считает — решение о кандидате зависит от базы, а не от его статуса"


# ---------------------------------------------------------------------------
# Половина 2: каталог/поиск/запись не читают статус связи
# ---------------------------------------------------------------------------

def test_visibility_surfaces_do_not_read_mapping_status():
    offenders = []
    scanned = 0
    for rel, path in _py_files(*VISIBILITY_SURFACES):
        scanned += 1
        for hit in _reads_mapping_status(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{rel}:{hit}")
    assert scanned >= 6, f"просканировано {scanned} файлов — корень не тот"
    assert not offenders, (
        "каталог/поиск/запись читают статус связи (§10.1: catalog_visible ≠ recommendation_eligible):\n  "
        + "\n  ".join(offenders)
    )


def test_the_scanner_sees_the_one_reader_that_exists():
    """Положительный контроль: без него «нарушителей нет» = «не искал»."""
    hits = _reads_mapping_status(ast.parse((ROOT / RESOLVER_SOURCE).read_text(encoding="utf-8")))
    assert hits, f"{RESOLVER_SOURCE} читает mapping_status, а сканер этого не видит"


# ---------------------------------------------------------------------------
# Поведение видимости: UNMAPPED виден в поиске и в карточке
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_unmapped_service_is_searchable_and_on_the_card():
    from rest_framework.test import APIClient

    from services.models import SalonService, ServiceCategory, SpecialistService
    from tenants.models import Tenant
    from users.models import SpecialistProfile, User

    tenant = Tenant.objects.create(slug="c2-vis", name="C2")
    user = User.objects.create_user(username="c2-master", password="x", role="specialist", phone="+79990001900")
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = "Видимая"
    profile.tenant = tenant
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    cat = ServiceCategory.objects.create(name="Массаж C2", slug="massage-c2")
    salon = SalonService.objects.create(
        tenant=tenant, category=cat, name="Массаж спины C2", duration_minutes=60,
        mapping_status=SalonService.MappingStatus.UNMAPPED,
    )
    SpecialistService.objects.create(
        salon_service=salon, specialist=profile, price=Decimal("1500"), duration_minutes=60,
    )

    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "client"
    viewer = User.objects.create_user(username="c2-client", password="x", role="client", phone="+79990001901")
    client.force_authenticate(user=viewer)

    resp = client.get("/api/v1/search/", {"q": "Массаж спины C2"})
    assert resp.status_code == 200, resp.content[:500]
    found = resp.json()
    names = {s["display_name"] for s in found["data"]["specialists"]} | {
        s["name"] for s in found["data"]["services"]
    }
    assert {"Видимая", "Массаж спины C2"} <= names, found

    resp = client.get(f"/api/v1/specialists/{profile.id}/")
    assert resp.status_code == 200, resp.content[:500]
    card = resp.json()  # ViewSet: без конверта {"data": ...}
    assert [s["name"] for s in card["services"]] == ["Массаж спины C2"]
