"""HTTP-примитивы smoke-runner'а: Ayla internal, bot customer (MaxInitData), ingest (HMAC).

Все вызовы возвращают SmokeResponse и НЕ бросают исключений на HTTP-ошибках —
раннер сам разводит PASS/FAIL/SKIP.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests

from .config import SmokeConfig


@dataclass
class SmokeResponse:
    status: int
    json_body: object          # dict/list или None
    text: str
    headers: requests.structures.CaseInsensitiveDict
    error: str = ""            # сетевая ошибка (status == 0)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class Check:
    scenario: str
    name: str
    status: str                # PASS | FAIL | SKIP
    detail: str = ""


PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class SmokeHttp:
    def __init__(self, cfg: SmokeConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "pilot-smoke/1.0 (W6)"})

    # -- transport ---------------------------------------------------------
    def _req(self, method: str, url: str, **kw) -> SmokeResponse:
        kw.setdefault("timeout", self.cfg.timeout)
        try:
            r = self.session.request(method, url, **kw)
        except requests.RequestException as exc:
            return SmokeResponse(0, None, "", {}, error=str(exc))
        body = None
        try:
            body = r.json()
        except ValueError:
            pass
        return SmokeResponse(r.status_code, body, r.text[:2000], r.headers)

    # -- Ayla internal API --------------------------------------------------
    def ayla(self, method: str, path: str, *, json_body=None, external_user: str = "",
             idempotency_key: str = "", extra_headers: dict | None = None) -> SmokeResponse:
        headers = {"Authorization": f"Bearer {self.cfg.ayla_token}"}
        if external_user:
            headers["X-External-User-ID"] = external_user
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        if self.cfg.tenant_slug:
            headers["X-Tenant"] = self.cfg.tenant_slug
        if extra_headers:
            headers.update(extra_headers)
        return self._req(method, f"{self.cfg.ayla_base_url}{path}",
                         json=json_body, headers=headers)

    def ayla_health(self) -> SmokeResponse:
        return self._req("GET", f"{self.cfg.ayla_base_url}/api/v1/health/")

    # -- Bot customer/master API (MaxInitData) ------------------------------
    @staticmethod
    def mint_init_data(bot_token: str, max_user_id: int) -> str:
        """Минт MAX webview initData (HMAC-схема, как в apps/miniapp_api/tests)."""
        pairs = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": max_user_id}, separators=(",", ":")),
        }
        data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        pairs["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        return urlencode(pairs)

    def bot(self, method: str, path: str, *, init_data: str = "", json_body=None) -> SmokeResponse:
        headers = {}
        if init_data:
            headers["Authorization"] = f"MaxInitData {init_data}"
        return self._req(method, f"{self.cfg.bot_base_url}{path}",
                         json=json_body, headers=headers)

    # -- Bot events ingest (HMAC, без trailing slash!) ----------------------
    def ingest(self, raw_body: bytes) -> SmokeResponse:
        ts = str(int(time.time() * 1000))
        sig = hmac.new(self.cfg.hmac_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Ayla-Event-Signature": f"sha256={sig}",
            "X-Ayla-Event-Timestamp": ts,
        }
        return self._req("POST", f"{self.cfg.bot_base_url}/api/v1/internal/events/ingest",
                         data=raw_body, headers=headers)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def new_event_envelope(event_name: str, data: dict, *, version: int = 1,
                           tenant_id=None, user_id: str = "") -> bytes:
        env = {
            "event_id": str(uuid.uuid4()),
            "event_name": event_name,
            "event_version": version,
            "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tenant_id": tenant_id,
            "user_id": user_id or str(uuid.uuid4()),
            "actor": "system",
            "correlation_id": str(uuid.uuid4()),
            "causation_id": None,
            "data": data,
        }
        return json.dumps(env).encode()


def unwrap_data(resp: SmokeResponse):
    """Снять envelope {"data": …} если есть."""
    if isinstance(resp.json_body, dict) and "data" in resp.json_body:
        return resp.json_body["data"]
    return resp.json_body


def pick_first(payload) -> dict | None:
    """Первый элемент list или paginated {"results": [...]}."""
    if isinstance(payload, dict):
        items = payload.get("results") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return items[0] if items else None
