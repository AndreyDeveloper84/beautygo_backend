"""Конфигурация pilot smoke-runner'а — только переменные окружения.

Никаких секретов в коде. Пример: см. README.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class SmokeConfig:
    """Все внешние зависимости раннера."""

    ayla_base_url: str          # https://dev.gobeauty.site
    bot_base_url: str           # https://bot-staging…
    ayla_token: str             # AYLA_INTERNAL_API_TOKEN (internal Bearer на Ayla)
    hmac_secret: str            # AYLA_OUTBOUND_HMAC_SECRET == EVENT_INGEST_HMAC_SECRET бота
    max_bot_token: str          # MAX_BOT_TOKEN — для минта MaxInitData (customer/master API)
    bot_db_dsn: str             # postgres://… — опционально, SQL-пробы (dedupe/reminders)
    client_id: str              # Ayla User UUID синтетического клиента (если известен заранее)
    specialist_id: str          # SpecialistProfile UUID (иначе — dynamic discovery)
    service_id: str             # Service UUID (иначе — dynamic discovery)
    bot_master_id: str          # bot-side master id (иначе bot-лег booking SKIP)
    bot_service_id: str         # bot-side service id
    tenant_slug: str            # X-Tenant, если на staging MULTI_TENANT_STRICT=true
    timeout: float              # HTTP timeout, сек

    @classmethod
    def from_env(cls) -> "SmokeConfig":
        return cls(
            ayla_base_url=_env("AYLA_BASE_URL").rstrip("/"),
            bot_base_url=_env("BOT_BASE_URL").rstrip("/"),
            ayla_token=_env("AYLA_INTERNAL_API_TOKEN"),
            hmac_secret=_env("AYLA_OUTBOUND_HMAC_SECRET") or _env("EVENT_INGEST_HMAC_SECRET"),
            max_bot_token=_env("MAX_BOT_TOKEN"),
            bot_db_dsn=_env("BOT_DB_DSN"),
            client_id=_env("SMOKE_CLIENT_ID"),
            specialist_id=_env("SMOKE_SPECIALIST_ID"),
            service_id=_env("SMOKE_SERVICE_ID"),
            bot_master_id=_env("SMOKE_BOT_MASTER_ID"),
            bot_service_id=_env("SMOKE_BOT_SERVICE_ID"),
            tenant_slug=_env("SMOKE_TENANT_SLUG"),
            timeout=float(_env("SMOKE_TIMEOUT", "15")),
        )

    @property
    def has_ayla(self) -> bool:
        return bool(self.ayla_base_url and self.ayla_token)

    @property
    def has_bot(self) -> bool:
        return bool(self.bot_base_url and self.max_bot_token)

    @property
    def has_ingest(self) -> bool:
        return bool(self.bot_base_url and self.hmac_secret)
