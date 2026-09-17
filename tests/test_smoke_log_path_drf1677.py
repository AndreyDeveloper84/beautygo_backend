"""Журнал smoke не пишется в `/app` образа — DRF-1677, класс DRF-1646.

`pytest.ini` задаёт `log_file` ОТНОСИТЕЛЬНЫМ путём, а `WORKDIR` образа — `/app`,
принадлежащий root со сборки: в `Dockerfile` нет ни одной директивы `USER`, а
`entrypoint.sh` владельца не чинит. Пока контейнер шёл от root, это работало.

`ea96e04` (11.09 17:34) перевёл web и celery на `user: "${APP_UID:-1000}…"` — и
следующий же ночной прогон упал ещё ДО сбора тестов::

    INTERNALERROR> PermissionError: [Errno 13] Permission denied: '/app/test_results.log'

Четыре подряд: 12, 13, 14, 15 сентября; последний зелёный — 11.09 08:43, то есть
до правки. Каталог `/app` при этом записываем (соседний шаг делает
``mkdir -p /app/analytics`` под тем же uid и проходит) — не открывается именно
ЧУЖОЙ файл, оставшийся от эпохи root-контейнера.

Почему не фикстурой, как у соседнего DRF-1663: `log_file` читается pytest'ом из
ini при инициализации логгера, до того как отработает хоть одна фикстура.
Автоuse-фикстура сюда не дотягивается по устройству, а не по недосмотру —
приём чинит свой слой, а этот дефект лежит слоем ниже.

**Что держит сторож:** вызов smoke переопределяет `log_file` на путь ВНЕ `/app`.
Проверяется свойство пути, а не наличие подстроки: переезд журнала на любой
другой годный каталог сторож переживёт молча, а возврат в `/app` или пропажа
переопределения — покраснеют.

**Названный предел:** сторож НЕ доказывает, что путь действительно записываем в
контейнере. Это свойство хоста, а не репозитория; на сегодня `/tmp` взят как
1777 по построению `python:3.12-slim` и живьём не измерен.
"""

from __future__ import annotations

import re
from pathlib import Path
from posixpath import normpath

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / ".github" / "workflows" / "smoke-on-dev.yml"
PYTEST_INI = REPO / "pytest.ini"

#: Рабочий каталог образа (`Dockerfile`: `WORKDIR /app`). Относительный
#: `log_file` разрешается именно отсюда.
IMAGE_WORKDIR = "/app"


def _smoke_script() -> str:
    """Тело ssh-шага, который гоняет smoke-сюиту."""
    doc = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
    steps = doc["jobs"]["smoke"]["steps"]
    host = next((s for s in steps if "pytest" in str(s.get("with", {}))), None)
    assert host is not None, "в smoke-on-dev не осталось шага, зовущего pytest — сторож смотрит не туда"
    return host["with"]["script"]


def _smoke_invocation(script: str) -> str:
    """Вызов smoke-сюиты одной строкой: продолжения через `\\` склеены.

    Без склейки переопределение, перенесённое на следующую строку, выглядело бы
    отсутствующим — и сторож краснел бы на верной правке.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", script)
    line = next((ln for ln in joined.splitlines() if "pytest nutrition/tests/smoke" in ln), None)
    assert line is not None, "вызов smoke-сюиты исчез — проверять нечего"
    return line


def _effective_log_file(invocation: str) -> str | None:
    """Путь журнала, который реально получит pytest, либо ``None``.

    ``None`` значит «переопределения нет» — тогда действует относительный путь
    из `pytest.ini`, то есть журнал уходит в ``WORKDIR`` образа.
    """
    found = re.findall(r"-o\s+log_file=(\S+)", invocation)
    return found[-1] if found else None


def test_the_ini_still_resolves_the_log_into_the_image_workdir() -> None:
    """Положительная стража: без неё всё ниже зелено и на ini без `log_file` вовсе.

    Сторож существует ровно потому, что путь в ini относительный. Стань он
    абсолютным и годным — переопределение перестанет быть нужным, и этот узел
    скажет, что предмет изменился, вместо того чтобы молча остаться верным.
    """
    text = PYTEST_INI.read_text(encoding="utf-8")
    declared = re.search(r"^\s*log_file\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    assert declared is not None, "в pytest.ini не стало log_file — предмет сторожа исчез, сторож снять"
    assert not declared.group(1).startswith("/"), (
        "log_file в pytest.ini стал абсолютным — переопределение в smoke, возможно, "
        "больше не нужно; проверить и пересмотреть этот сторож"
    )


def test_the_smoke_run_keeps_the_log_out_of_the_image_workdir() -> None:
    """Свойство, а не подстрока: действующий путь журнала лежит ВНЕ `/app`."""
    invocation = _smoke_invocation(_smoke_script())
    log_file = _effective_log_file(invocation)

    assert log_file is not None, (
        "pytest в smoke идёт без `-o log_file=…`: относительный путь из pytest.ini "
        f"разрешится в {IMAGE_WORKDIR} образа, где процесс под `user:` писать не может "
        "(DRF-1677) — прогон упадёт INTERNALERROR'ом до сбора тестов"
    )

    resolved = log_file if log_file.startswith("/") else f"{IMAGE_WORKDIR}/{log_file}"
    resolved = normpath(resolved)
    assert resolved != IMAGE_WORKDIR and not resolved.startswith(f"{IMAGE_WORKDIR}/"), (
        f"журнал smoke снова разрешается внутрь {IMAGE_WORKDIR}: {log_file!r} → {resolved!r}"
    )


@pytest.mark.parametrize(
    ("value", "outside"),
    [
        ("/tmp/test_results.log", True),
        ("/var/tmp/x.log", True),
        ("/app/test_results.log", False),
        ("test_results.log", False),
        ("/app/../tmp/x.log", True),
        ("/app/sub/../../tmp/x.log", True),
    ],
)
def test_the_property_itself_separates_inside_from_outside(value: str, outside: bool) -> None:
    """Сторож сторожа: проверка отличает «внутри» от «снаружи», включая `..`.

    Без этого узла предыдущий тест зелен и при сломанной логике разбора —
    например, если бы `..` не сворачивалось и `/app/../tmp` считалось внутренним.
    """
    resolved = normpath(value if value.startswith("/") else f"{IMAGE_WORKDIR}/{value}")
    is_outside = resolved != IMAGE_WORKDIR and not resolved.startswith(f"{IMAGE_WORKDIR}/")
    assert is_outside is outside, f"{value!r} → {resolved!r}"
