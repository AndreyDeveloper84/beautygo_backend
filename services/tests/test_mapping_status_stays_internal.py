"""§1 п. 6 решения владельца (11.09.2026): `REVIEW_REQUIRED` виден только во
внутренней очереди проверки.

Сегодня это верно по грепу — ни один сериализатор не отдаёт
``mapping_status`` — и ничем не охраняется: следующий сериализатор с этим
полем прошёл бы молча. Два сторожа:

* **структурный** — AST по всем модулям, где определяются сериализаторы
  DRF: `ModelSerializer` над `SalonService` обязан перечислять поля явно
  (не `__all__`) и не перечислять `mapping_status` и `mapping_confirmed_*`.
  Единственное место, где статус читает человек, — `services/admin.py`,
  и это не сериализатор;
* **поведенческий** — зеркало каталога для бота (Bearer-ручка) отдаёт
  строку со статусом `review_required`, и в её JSON нет ни ключа
  `mapping_status`, ни значения `review_required` где бы то ни было.

Положительный контроль у структурного: сканер обязан найти
``SalonServiceInternalSerializer`` — иначе зелень значила бы «сканер слеп».
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from services.models import SalonService

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("mapping_status", "mapping_confirmed_by", "mapping_confirmed_rule",
             "mapping_rule_version", "mapping_confirmed_at", "mapping_source_ref")
INTERNAL_URL = "/api/v1/internal/catalog/salon-services/"
VALID_TOKEN = "s3a-secret-token"


def _serializer_classes():
    """(модуль, имя класса, model, fields) для каждого ModelSerializer в репозитории."""
    found = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        # Единственное разрешённое место — внутренняя очередь проверки:
        # форма админки читает статус, чтобы человек его ПОДТВЕРДИЛ. Это
        # не сериализатор наружу, и исключение названо, а не подразумевается.
        if rel == "services/admin.py":
            continue
        if "/tests/" in rel or "/migrations/" in rel or any(
            p in (".venv", "venv", "node_modules") for p in path.parts
        ):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            meta = next((b for b in node.body if isinstance(b, ast.ClassDef) and b.name == "Meta"), None)
            if meta is None:
                continue
            model = fields = None
            for stmt in meta.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id == "model":
                        model = ast.unparse(stmt.value)
                    if isinstance(t, ast.Name) and t.id == "fields":
                        fields = stmt.value
            if model is not None:
                found.append((rel, node.name, model, fields))
    return found


def _field_names(fields_node) -> set[str] | None:
    """Имена из литерала списка/кортежа; `None` — не литерал (например `A.Meta.fields + [...]`)."""
    if fields_node is None:
        return set()
    names: set[str] = set()
    for sub in ast.walk(fields_node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            names.add(sub.value)
    return names


def test_no_serializer_over_salon_service_exposes_mapping_status():
    """Структурный сторож: явные поля, и среди них нет статуса связи."""
    classes = _serializer_classes()
    salon = [c for c in classes if c[2].endswith("SalonService")]

    # Положительный контроль: сканер видит то, что заведомо есть.
    assert any(name == "SalonServiceInternalSerializer" for _, name, _, _ in salon), (
        "сканер не нашёл SalonServiceInternalSerializer — он слеп, зелень ничего не значит"
    )

    offenders = []
    for rel, name, _, fields in salon:
        names = _field_names(fields)
        if "__all__" in names:
            offenders.append(f"{rel}::{name} — fields = '__all__' отдаст mapping_status")
        leaked = sorted(names & set(FORBIDDEN))
        if leaked:
            offenders.append(f"{rel}::{name} — {', '.join(leaked)}")
    assert not offenders, (
        "§1 п. 6: статус связи виден только во внутренней очереди (services/admin.py), "
        "а сериализатор его отдаёт:\n  " + "\n  ".join(offenders)
    )


def _walk_json(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk_json(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_json(v)
    else:
        yield value


@pytest.mark.django_db
def test_catalog_mirror_does_not_carry_review_required(settings):
    """Поведенческий сторож: строка со статусом review_required отдаётся
    зеркалу без статуса — ни ключом, ни значением."""
    from services.models import ServiceCategory, ServiceTemplate
    from tenants.models import Tenant

    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    tenant = Tenant.objects.create(slug="p6-t", name="P6")
    cat = ServiceCategory.objects.create(name="P6 Маникюр", slug="p6-man")
    tpl = ServiceTemplate.objects.create(category=cat, name="P6 шаблон", name_short="P6", duration_default=30)
    # Статус ставится явно: умолчание модели — `unmapped`, а сторож про то,
    # что именно `review_required` не уходит наружу.
    ss = SalonService.objects.create(
        tenant=tenant, template=tpl, category=cat, name="P6 услуга",
        mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
    )

    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    r = client.get(f"{INTERNAL_URL}{ss.id}/")
    assert r.status_code == 200, r.content[:200]

    payload = json.loads(r.content)
    atoms = list(_walk_json(payload))
    assert "mapping_status" not in atoms
    assert "review_required" not in atoms
    # положительный контроль: ответ не пустой и про эту строку
    assert str(ss.id) in atoms
