"""Стадия 7 — persistence authorization. В этом PR — только отказ.

Запись решений резолвера (MAP-AUTO-06) требует: слова владельца OD-NEW-7
(apply authority), включённых правил OD-NEW-1/2, готовой схемы и
parity-проверки. Ничего из этого здесь не принимается: ``authorize_apply``
всегда отказывает и называет, чего именно нет. Заглушка существует, чтобы
команда имела один вход для ``--apply`` и отвечала отказом, а не молчала.
"""
from __future__ import annotations


class ApplyNotAuthorized(RuntimeError):
    pass


def authorize_apply(*, tenant_slug: str) -> None:
    raise ApplyNotAuthorized(
        f"apply для «{tenant_slug}» не разрешён: OD-NEW-7 (apply authority) не принят, "
        "OD-NEW-1/2 (правила R2/R1) не приняты; запись — MAP-AUTO-06, не этот срез. "
        "Dry-run доступен без ограничений."
    )
