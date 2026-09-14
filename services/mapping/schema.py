"""Готовность схемы — стоп до чтения корпуса (план §10 «stale schema»).

«Замер без пульса не принимается»: команда печатает, на какой базе она
работает (имя, host, старт постмастера), и проверяет, что миграции
``services.0023`` (поле ``canonical_code``) и ``0024`` (bootstrap) применены и
что у канона хоть один код есть. Иначе резолвер сравнивал бы с пустым
``canonical_code`` и честно выдавал бы ``BLOCKED(TEMPLATE_WITHOUT_CODE)`` на
каждой строке — верно, но не о том.

Полная версия с сверкой адреса пилота (C-1: ``ruvds-o1mqo`` vs
``ruvds-l2wyz``) — WP-02 (b); здесь минимум, достаточный для dry-run.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

REQUIRED_MIGRATIONS = (
    ("services", "0023_servicetemplate_canonical_code"),
    ("services", "0024_canonical_code_bootstrap"),
)


class SchemaNotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class Subject:
    db_name: str
    db_host: str
    postmaster_started_at: str
    templates_with_code: int

    def line(self) -> str:
        return (
            f"предмет: база {self.db_name}@{self.db_host or 'local'} · postmaster с {self.postmaster_started_at} · "
            f"канонов с кодом: {self.templates_with_code}"
        )


def describe_subject() -> Subject:
    from services.models import ServiceTemplate

    started = "n/a"
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_postmaster_start_time()")
            started = cur.fetchone()[0].isoformat(timespec="seconds")
    return Subject(
        db_name=connection.settings_dict.get("NAME", ""),
        db_host=connection.settings_dict.get("HOST", "") or "",
        postmaster_started_at=started,
        templates_with_code=ServiceTemplate.objects.filter(canonical_code__isnull=False).count(),
    )


def assert_schema_ready() -> Subject:
    applied = {(m.app, m.name) for m in MigrationRecorder(connection).migration_qs.filter(app="services")}
    missing = [f"{app}.{name}" for app, name in REQUIRED_MIGRATIONS if (app, name) not in applied]
    if missing:
        raise SchemaNotReady("схема не готова: не применены " + ", ".join(missing))
    subject = describe_subject()
    if subject.templates_with_code == 0:
        raise SchemaNotReady(
            "схема не готова: ни один канон не несёт canonical_code — bootstrap 0024 на этой базе "
            "ничего не поставил (пустой справочник?)"
        )
    return subject
