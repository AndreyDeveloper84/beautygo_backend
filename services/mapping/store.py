"""Хранилище отчёта dry-run — файл, не модель (план §3 MAP-AUTO-05: «предпочтение файлу»).

Один файл на салон: ``<MAPPING_REPORT_DIR>/<tenant_slug>.json``. Админка
читает его и показывает исход резолвера рядом с услугой; пересчёт по
кнопке перезаписывает файл. База при этом не трогается ни чтением статуса
сверх ``SKIP_DECIDED``, ни записью — отчёт живёт вне схемы, и «нет
прогона» отличимо от «прогон был, исхода нет».

## Право записи — часть контракта (DRF-2409)

Каталог создаётся здесь же (``mkdir(parents=True)``), и на стенде этого
оказалось мало: ``/app`` принадлежит root, контейнер идёт под uid 1000, и
``mkdir`` падал голой ``PermissionError`` — ни пути, ни имени настройки, ни
того, что делать. Теперь невозможность писать — **названный отказ**
(:class:`ReportDirUnavailable`), а не трасса: она говорит путь и настройку,
которой он меняется.

Проверяется именно ПРАВО записи, а не существование: каталог, созданный
root, существует и не пишется — ровно то состояние, что сломало стенд.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from services.mapping.types import RULE_VERSION, ResolverRow, Summary


class ReportDirUnavailable(RuntimeError):
    """Каталог отчётов недоступен на запись — с путём и именем настройки."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"каталог отчётов недоступен на запись: {path} ({reason}). "
            f"Он задаётся настройкой MAPPING_REPORT_DIR (переменная окружения "
            f"MAPPING_REPORT_DIR); внутри образа умолчание — /app/var/mapping_reports, "
            f"и его владелец должен совпадать с APP_UID контейнера."
        )
        self.path = path


def report_dir() -> Path:
    return Path(getattr(settings, "MAPPING_REPORT_DIR", settings.BASE_DIR / "var" / "mapping_reports"))


def ensure_report_dir() -> Path:
    """Каталог есть И пишется — или названный отказ.

    Пробой записи, а не ``os.access``: под контейнером права решает не только
    режим каталога (том только на чтение, чужой владелец, переполненный диск),
    и единственный честный ответ на вопрос «сможем ли записать» — попробовать.
    """
    d = report_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportDirUnavailable(d, f"создать не вышло: {exc.strerror or exc}") from exc
    probe = d / ".write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ReportDirUnavailable(d, f"писать нельзя: {exc.strerror or exc}") from exc
    return d


def report_path(tenant_slug: str) -> Path:
    return report_dir() / f"{tenant_slug}.json"


def store_report(rows: list[ResolverRow], tenant_slug: str, *, rules: str) -> Path:
    ensure_report_dir()
    path = report_path(tenant_slug)
    payload = {
        "tenant": tenant_slug,
        "generated_at": timezone.now().isoformat(timespec="seconds"),
        "rule_version": RULE_VERSION,
        "rules": rules,
        "summary": asdict(Summary.of(rows)),
        "rows": {str(r.salon_service_id): asdict(r) for r in rows},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


class StoredReport:
    """Отчёт последнего прогона по салону; ``None`` в ``load`` = прогона не было."""

    def __init__(self, payload: dict, path: Path) -> None:
        self.payload = payload
        self.path = path
        self.rows: dict[str, dict] = payload.get("rows", {})

    @property
    def generated_at(self) -> datetime | None:
        raw = self.payload.get("generated_at")
        return datetime.fromisoformat(raw) if raw else None

    @property
    def rules(self) -> str:
        return self.payload.get("rules") or "—"

    def row(self, salon_service_id) -> dict | None:
        return self.rows.get(str(salon_service_id))

    @classmethod
    def load(cls, tenant_slug: str) -> "StoredReport | None":
        path = report_path(tenant_slug)
        if not path.exists():
            return None
        return cls(json.loads(path.read_text(encoding="utf-8")), path)

    @classmethod
    def load_all(cls) -> dict[str, "StoredReport"]:
        d = report_dir()
        if not d.exists():
            return {}
        out = {}
        for p in sorted(d.glob("*.json")):
            out[p.stem] = cls(json.loads(p.read_text(encoding="utf-8")), p)
        return out
