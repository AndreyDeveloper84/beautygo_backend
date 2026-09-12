"""Всё, что процесс пишет при старте под ``user:``, лежит на смонтированном пути (DRF-1677).

### Класс

#354 поставил ``user: 1000:1000`` на web / celery_worker / celery_beat, чтобы
entrypoint не оставлял root-файлы в дереве. Под root любая запись внутрь
образа проходила молча; под uid дерева каталоги образа (root, 755)
закрыты. Ночь 12.09.2026: ``dev-celery_beat-1`` — 67 рестартов, exit 1
сразу после «db → celerybeat-schedule»: PersistentScheduler пишет shelve
в CWD (``/app``) — ни одной периодической задачи каталога (outbox 10 с,
publish-to-bot 30 с, purge). Тот же класс у surface_state (#383): запись в
``/app/docs/generated`` внутри образа.

### Инвариант

Для каждого сервиса с ``user:`` каждый путь, который его процесс пишет
при старте, лежит под объявленным в compose монтированием этого сервиса —
bind (``volumes``) или tmpfs с режимом 1777. Таблица стартовых записей
ниже — **знание о процессах**, не о compose; она устаревает молча, и
потому у каждой строки есть источник.

### Чего сторож не видит — названо

* Записи, которых нет в таблице (новый флаг ``--pidfile``, логгер в файл).
  Таблица — предел сторожа, не доказательство закрытой границы.
* Права на самом хосте: bind-mount в каталог, которого нет, docker создаёт
  от root. Это стережётся у шага деплоя (mkdir до run), не здесь.
* Режим tmpfs «в бою»: замерено 12.09 (python:3.12-slim под 1000:1000):
  без ``mode`` — 755 root, denied; ``mode: 1777`` compose читает как
  десятичное; работает ``01777``. Сторож проверяет число, не поведение.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile"
TMPFS_WORLD_WRITABLE = 0o1777

# Стартовые записи каждого процесса под user:. Источник — рядом.
#   beat:   celery.beat.PersistentScheduler → shelve по --schedule,
#           умолчание ./celerybeat-schedule в CWD (= WORKDIR образа).
#   worker: пишет файлы только по явным --pidfile / --statedb / --logfile.
#   web:    entrypoint.sh — collectstatic → STATIC_ROOT (/app/staticfiles),
#           загрузки → MEDIA_ROOT (/app/media).
BEAT_DEFAULT_SCHEDULE = "celerybeat-schedule"
WORKER_FILE_FLAGS = ("--pidfile", "--statedb", "--logfile")
WEB_WRITE_DIRS = ("/app/staticfiles", "/app/media")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _workdir() -> str:
    lines = [ln.split()[1] for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines() if ln.startswith("WORKDIR ")]
    assert lines, "в Dockerfile нет WORKDIR"
    return lines[-1].rstrip("/")


def _mounts(service: dict) -> dict[str, str]:
    """{контейнерный путь: 'bind' | 'tmpfs-<mode>'} по volumes сервиса."""
    out: dict[str, str] = {}
    for vol in service.get("volumes") or []:
        if isinstance(vol, str):
            parts = vol.split(":")
            if len(parts) >= 2:
                out[parts[1].rstrip("/")] = "bind"
        elif isinstance(vol, dict) and vol.get("type") == "tmpfs":
            mode = (vol.get("tmpfs") or {}).get("mode")
            out[str(vol["target"]).rstrip("/")] = f"tmpfs-{mode}"
        elif isinstance(vol, dict) and vol.get("type") == "bind":
            out[str(vol["target"]).rstrip("/")] = "bind"
    for t in service.get("tmpfs") or []:
        out[str(t).split(":")[0].rstrip("/")] = "tmpfs-None"
    return out


def _covering_mount(path: str, mounts: dict[str, str]) -> str | None:
    for m in sorted(mounts, key=len, reverse=True):
        if path == m or path.startswith(m + "/"):
            return m
    return None


def _command_args(service: dict) -> list[str]:
    cmd = service.get("command")
    if cmd is None:
        return []
    return list(cmd) if isinstance(cmd, list) else shlex.split(cmd)


def _flag_value(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _startup_write_paths(name: str, service: dict, workdir: str) -> list[str]:
    """Пути, которые процесс сервиса пишет при старте (по таблице выше)."""
    args = _command_args(service)
    if name == "celery_beat" or "beat" in args:
        sched = _flag_value(args, "--schedule") or _flag_value(args, "-s") or BEAT_DEFAULT_SCHEDULE
        if not sched.startswith("/"):
            sched = f"{workdir}/{sched}"
        return [sched]
    if name == "celery_worker" or "worker" in args:
        paths = [_flag_value(args, f) for f in WORKER_FILE_FLAGS]
        return [f"{workdir}/{p}" if not p.startswith("/") else p for p in paths if p]
    if name == "web":
        return list(WEB_WRITE_DIRS)
    return []


def _violations(compose: dict, workdir: str) -> dict[str, list[str]]:
    """{сервис: [описание]} — стартовые записи под user:, не накрытые монтированием."""
    out: dict[str, list[str]] = {}
    for name, svc in compose["services"].items():
        if not svc.get("user"):
            continue
        mounts = _mounts(svc)
        problems: list[str] = []
        for path in _startup_write_paths(name, svc, workdir):
            target = path if path in WEB_WRITE_DIRS else path.rsplit("/", 1)[0]
            m = _covering_mount(target, mounts)
            if m is None:
                problems.append(f"{path}: каталог образа, не смонтирован")
            elif mounts[m].startswith("tmpfs-") and mounts[m] != f"tmpfs-{TMPFS_WORLD_WRITABLE}":
                problems.append(f"{path}: tmpfs {m} с mode={mounts[m][6:]}, нужен 01777 (= {TMPFS_WORLD_WRITABLE})")
        if problems:
            out[name] = problems
    return out


# ─── сторож ─────────────────────────────────────────────────────────────────


def test_every_startup_write_of_a_user_service_lands_on_a_mount() -> None:
    compose = _compose()
    with_user = [n for n, s in compose["services"].items() if s.get("user")]
    # Перепись до «нарушителей нет»: три сервиса #354 обязаны быть под user:.
    assert {"web", "celery_worker", "celery_beat"} <= set(with_user), with_user

    assert _violations(compose, _workdir()) == {}, (
        "процесс под user: пишет при старте внутрь образа — под uid дерева это EACCES и crashloop, "
        "как у celery_beat 12.09 (67 рестартов, расписание каталога стояло)"
    )


def test_beat_carries_an_explicit_schedule_path_on_a_world_writable_tmpfs() -> None:
    """Прямая форма: флаг есть, каталог — tmpfs 01777. Иначе умолчание — CWD образа."""
    beat = _compose()["services"]["celery_beat"]
    sched = _flag_value(_command_args(beat), "--schedule")
    assert sched, "beat без --schedule пишет ./celerybeat-schedule в /app — каталог образа"
    mounts = _mounts(beat)
    assert mounts.get(sched.rsplit("/", 1)[0]) == f"tmpfs-{TMPFS_WORLD_WRITABLE}", mounts


def test_worker_writes_no_files_at_start() -> None:
    """Пока у worker нет --pidfile/--statedb/--logfile, ему нечего монтировать; появятся — попадут под сторож."""
    worker = _compose()["services"]["celery_worker"]
    assert _startup_write_paths("celery_worker", worker, _workdir()) == []


# ─── положительный контроль ─────────────────────────────────────────────────


def _svc(command: str, volumes: list | None = None) -> dict:
    return {"user": "1000:1000", "command": command, "volumes": volumes or []}


@pytest.mark.parametrize(
    ("service", "expect_violation"),
    [
        # форма ночи 12.09: beat без --schedule, монтирований нет
        (_svc("celery -A x beat --loglevel=info"), True),
        # tmpfs без режима — 755 root, denied (замер 12.09)
        (
            _svc("celery -A x beat --schedule /app/s/f", [{"type": "tmpfs", "target": "/app/s"}]),
            True,
        ),
        # mode: 1777 десятичное — тоже denied
        (
            _svc(
                "celery -A x beat --schedule /app/s/f",
                [{"type": "tmpfs", "target": "/app/s", "tmpfs": {"mode": 1777}}],
            ),
            True,
        ),
        # правильная форма
        (
            _svc(
                "celery -A x beat --schedule /app/s/f",
                [{"type": "tmpfs", "target": "/app/s", "tmpfs": {"mode": 0o1777}}],
            ),
            False,
        ),
        # bind-mount тоже годится
        (_svc("celery -A x beat --schedule /app/s/f", ["/host/s:/app/s"]), False),
        # соседний каталог не считается
        (_svc("celery -A x beat --schedule /app/s/f", ["/host/t:/app/t"]), True),
        # worker с --pidfile внутрь образа
        (_svc("celery -A x worker --pidfile /app/w.pid"), True),
        (_svc("celery -A x worker"), False),
    ],
)
def test_the_checker_flags_unmounted_startup_writes(service: dict, expect_violation: bool) -> None:
    compose = {"services": {"celery_beat": service}}
    if "worker" in service["command"]:
        compose = {"services": {"celery_worker": service}}
    assert bool(_violations(compose, "/app")) is expect_violation, _violations(compose, "/app")


def test_the_checker_ignores_services_without_user() -> None:
    """Без user: процесс идёт от root образа и пишет куда угодно — класс другой (DRF-1646), не этот."""
    compose = {"services": {"celery_beat": {"command": "celery -A x beat"}}}
    assert _violations(compose, "/app") == {}
