"""DRF-1953 — перепись сериализаторов: имена колонок адреса на проводе читают место (§9).

Перепись L6 (``test_offer_address_l6.py``) считает обращения к колонкам
``address`` / ``location_lat`` / ``location_lng`` в коде, а свой предел
называет сама: строки на уровне класса она не видит. Этим пределом прошёл
``AppointmentSpecialistSerializer.address = serializers.CharField()`` —
объявленное поле без атрибута и без строки, читавшее адрес человека в
ответах записи.

Здесь — сторож КЛАССА, а не сегодняшнего написания, по всем сериализаторам
рабочего кода:

1. объявленное поле с именем колонки — только класс ``tenants.wire``;
2. имя колонки в ``Meta.fields`` сериализатора ``SpecialistProfile`` — только
   затенённое объявленным полем ``tenants.wire``;
3. ``source="….address"`` (и координаты) — запрещено.

Нижние границы: просканировано ≥ 400 файлов и объявлений ``tenants.wire``
увидено ≥ 12 — иначе «нарушений нет» значило бы «скан слеп».

Названный предел: имена, собранные динамически (``getattr`` со строкой из
переменной), ``SerializerMethodField``, читающий колонку в теле метода (его
считает перепись L6 как обращение к атрибуту), и сырой SQL.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLUMN_NAMES = frozenset({"address", "location_lat", "location_lng"})
WIRE_FIELD_CLASSES = frozenset({"OfferAddressField", "OfferLatitudeField", "OfferLongitudeField"})
SKIP_PARTS = frozenset({"tests", "migrations", "venv", ".venv", "node_modules"})
MIN_SCANNED = 400
MIN_WIRE_DECLARATIONS = 12


def _callee(node: ast.Call) -> str:
    return ast.unparse(node.func).split(".")[-1]


def _serializer_census() -> tuple[int, int, list[str]]:
    scanned, wire_seen, violations = 0, 0, []
    for path in sorted(ROOT.rglob("*.py")):
        rel_parts = path.relative_to(ROOT).parts
        if any(p in SKIP_PARTS or p.startswith(".") for p in rel_parts):
            continue
        scanned += 1
        rel = "/".join(rel_parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if not any("Serializer" in ast.unparse(b) for b in cls.bases):
                continue
            declared: dict[str, str] = {}
            for st in cls.body:
                if not (isinstance(st, ast.Assign) and len(st.targets) == 1
                        and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Call)):
                    continue
                name, callee = st.targets[0].id, _callee(st.value)
                declared[name] = callee
                if name in COLUMN_NAMES:
                    if callee in WIRE_FIELD_CLASSES:
                        wire_seen += 1
                    else:
                        violations.append(f"{rel}:{st.lineno} {cls.name}.{name} = {callee}(…) — не tenants.wire")
                for kw in st.value.keywords:
                    if (kw.arg == "source" and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            and kw.value.value.split(".")[-1] in COLUMN_NAMES):
                        violations.append(f"{rel}:{st.lineno} {cls.name}.{name} source={kw.value.value!r}")
            meta = next((n for n in cls.body if isinstance(n, ast.ClassDef) and n.name == "Meta"), None)
            if meta is None:
                continue
            model, fields = None, []
            for st in meta.body:
                if not (isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name)):
                    continue
                if st.targets[0].id == "model":
                    model = ast.unparse(st.value).split(".")[-1]
                if st.targets[0].id == "fields" and isinstance(st.value, (ast.List, ast.Tuple)):
                    fields = [e.value for e in st.value.elts
                              if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if model != "SpecialistProfile":
                continue
            for f in fields:
                if f in COLUMN_NAMES and declared.get(f) not in WIRE_FIELD_CLASSES:
                    violations.append(f"{rel}:{meta.lineno} {cls.name}.Meta.fields[{f!r}] — не затенено tenants.wire")
    return scanned, wire_seen, violations


def test_column_named_serializer_fields_read_the_place():
    scanned, wire_seen, violations = _serializer_census()
    assert scanned >= MIN_SCANNED, f"просканировано {scanned} файлов — корень не тот"
    assert wire_seen >= MIN_WIRE_DECLARATIONS, f"объявлений tenants.wire увидено {wire_seen} — скан слеп"
    assert violations == [], (
        "имя колонки адреса/координат на проводе читает профиль, а не место (§9, DRF-1953):\n  "
        + "\n  ".join(violations)
    )


def test_the_census_sees_a_plain_declared_address():
    """Самопроверка на известной форме: без неё «нарушений нет» зеленело бы и
    на скане, который не узнаёт объявленное поле."""
    src = (
        "from rest_framework import serializers\n"
        "class X(serializers.Serializer):\n"
        "    address = serializers.CharField()\n"
    )
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    st = cls.body[0]
    assert isinstance(st, ast.Assign) and st.targets[0].id in COLUMN_NAMES
    assert _callee(st.value) not in WIRE_FIELD_CLASSES
