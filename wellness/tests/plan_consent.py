"""Утверждение основания в телах узлов Плана — один источник.

Пишущие ручки Плана требуют в теле ``consent`` (``wellness/plan_gate.py``):
без него отвечает гейт, а не ручка. Узлы, чей предмет — сама ручка, берут
клиента отсюда: он дописывает утверждение в тело, если узел не положил своё.
Послать тело БЕЗ утверждения тем же клиентом — ``post(..., attested=False)``.

Зелёный прогон на этом клиенте не говорит, что ручка утверждение ТРЕБУЕТ:
это держат узлы гейта (``test_plan_gate.py``) — по узлу на ручку, без
подстановки.
"""

from __future__ import annotations

from rest_framework.test import APIClient

#: Годное утверждение: вид согласия, непустая версия текста, время с поясом.
ATTESTATION = {
    "consent": {"type": "personal_data", "document_version": "2026-10", "granted_at": "2026-10-01T09:00:00+00:00"},
}


class AttestingClient(APIClient):
    """Клиент узлов: в тело POST-вызова дописывается утверждение основания."""

    def post(self, path, data=None, format=None, *, attested: bool = True, **extra):
        if attested and isinstance(data, dict) and "consent" not in data:
            data = {**data, **ATTESTATION}
        return super().post(path, data, format=format, **extra)
