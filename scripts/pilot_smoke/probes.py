"""SQL-пробы для eventbus/R1 верификации (опциональная нога smoke — BOT_DB_DSN).

Имена таблиц/колонок сверены с моделями ai-bot-platform (2026-07-19):
- eventbus_ingestdedupe(event_id PK, event_name, event_version, received_at, processed_at)
- eventbus_ingestdlq(event_id, reason, dead_lettered_at; unique (event_id, reason))
- booking_bookingreminder(ayla_appointment_id, tenant_id, kind, status, scheduled_at, visit_at)
  идемпотентность R1: partial unique (ayla_appointment_id, tenant_id, kind)

NB: в старом runbook-рецепте (docs/runbooks/orders-rollback.md) колонка названа
`first_seen_at` — фактическое имя `received_at`. Здесь — исправленная версия.
"""
from __future__ import annotations

DEDUPE_BY_EVENT_ID = (
    "SELECT event_id, event_name, received_at, processed_at "
    "FROM eventbus_ingestdedupe WHERE event_id = %s;"
)

BOOKING_CREATED_SINCE = (
    "SELECT event_id, received_at FROM eventbus_ingestdedupe "
    "WHERE event_name = 'booking.created' AND received_at >= %s "
    "ORDER BY received_at DESC;"
)

DLQ_BY_EVENT_ID = (
    "SELECT event_id, reason, dead_lettered_at "
    "FROM eventbus_ingestdlq WHERE event_id = %s;"
)

REMINDERS_BY_APPOINTMENT = (
    "SELECT kind, status, scheduled_at, sent_at "
    "FROM booking_bookingreminder WHERE ayla_appointment_id = %s ORDER BY kind;"
)


class Probes:
    """Тонкая обёртка над psycopg2. Без DSN — все пробы SKIP с печатью SQL."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn = None

    @property
    def available(self) -> bool:
        return bool(self.dsn)

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self._conn:
            import psycopg2  # noqa: PLC0415 — опциональная зависимость
            self._conn = psycopg2.connect(self.dsn)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
