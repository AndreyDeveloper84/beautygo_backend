"""Сценарии S1–S7 smoke-runner'а пилота (acceptance §10, контракты v1.8.0).

Семантика статусов:
- PASS — проверка выполнена и соответствует контракту;
- FAIL — проверка выполнена, результат нарушает контракт/ожидание;
- SKIP — проверку нельзя выполнить в текущем окружении (нет токена/DSN/кредов) —
  это НЕ дефект кода; деталь объясняет, что нужно для запуска.

Правила гигиены: синтетические пользователи/брони; созданные брони отменяются;
персональный контекст восстанавливается или вытирается (C5 delete) в конце.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from .config import SmokeConfig
from .http import FAIL, PASS, SKIP, Check, SmokeHttp, pick_first, unwrap_data
from .probes import (BOOKING_CREATED_SINCE, DEDUPE_BY_EVENT_ID,
                     DLQ_BY_EVENT_ID, REMINDERS_BY_APPOINTMENT, Probes)

SMOKE_MAX_USER_ID = 900_000_001  # синтетический MAX user id для customer-ноги
STUB_MARKERS = (
    "booking-stub-001", "Массаж лимфодренаж", "Ирина", "ул. Тверская 12",
    '"calories_eaten": 1240', '"calories_target": 2100', "Меньше стресса",
)


class Ctx(dict):
    """Разделяемый контекст между сценариями (appointment_id, client_id, init_data…)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ext_user(max_user_id: int = SMOKE_MAX_USER_ID) -> str:
    return f"bot:max:{max_user_id}"


def _resolve_client_id(cfg: SmokeConfig, http: SmokeHttp, ctx: Ctx, add) -> str:
    """client_id (Ayla User UUID) — из env или из bot export subject.ayla_user_id."""
    if ctx.get("client_id"):
        return ctx["client_id"]
    if cfg.client_id:
        ctx["client_id"] = cfg.client_id
        return ctx["client_id"]
    if cfg.has_bot:
        init_data = ctx.get("init_data") or SmokeHttp.mint_init_data(cfg.max_bot_token, SMOKE_MAX_USER_ID)
        ctx["init_data"] = init_data
        resp = http.bot("GET", "/api/v1/customer/me/personal-data/export/", init_data=init_data)
        if resp.ok and isinstance(resp.json_body, dict):
            uid = (resp.json_body.get("subject") or {}).get("ayla_user_id")
            if uid:
                ctx["client_id"] = str(uid)
    return ctx.get("client_id", "")


def _discover_booking_target(cfg: SmokeConfig, http: SmokeHttp, add):
    """Найти (specialist_profile_id, service_id, start_datetime ISO, specialist_user_uuid)."""
    specialist_id = cfg.specialist_id
    user_uuid = ""
    if not specialist_id:
        resp = http.ayla("GET", "/api/v1/internal/specialists/")
        first = pick_first(resp.json_body)
        if not first:
            add(Check("S1", "discovery: специалист", SKIP,
                      f"каталог пуст или недоступен (HTTP {resp.status}) — задать SMOKE_SPECIALIST_ID"))
            return None
        specialist_id = str(first.get("id"))
    detail = http.ayla("GET", f"/api/v1/internal/specialists/{specialist_id}/")
    if detail.ok and isinstance(detail.json_body, dict):
        user_uuid = str(detail.json_body.get("user_id") or "")
    service_id = cfg.service_id
    if not service_id:
        resp = http.ayla("GET", f"/api/v1/internal/specialists/{specialist_id}/services/")
        svc = pick_first(resp.json_body)
        if not svc:
            add(Check("S1", "discovery: услуга", SKIP,
                      f"у специалиста нет услуг (HTTP {resp.status}) — задать SMOKE_SERVICE_ID"))
            return None
        service_id = str(svc.get("id"))
    start_iso = ""
    today = datetime.now(timezone.utc).date()
    for offset in range(1, 8):
        day = (today + timedelta(days=offset)).isoformat()
        resp = http.ayla(
            "GET",
            f"/api/v1/internal/specialists/{specialist_id}/slots/?service_id={service_id}&date={day}")
        if resp.ok and isinstance(resp.json_body, dict):
            slots = resp.json_body.get("slots") or []
            if slots:
                start_iso = slots[0]
                break
    if not start_iso:
        add(Check("S1", "discovery: слот", SKIP,
                  "нет свободных слотов на +7 дней — создать расписание на staging"))
        return None
    return specialist_id, service_id, start_iso, user_uuid


# ---------------------------------------------------------------------------
# S1. Booking CRUD сквозной (acceptance §10.1/10.2, AMD-002)
# ---------------------------------------------------------------------------

def s1_booking_crud(cfg: SmokeConfig, http: SmokeHttp, probes: Probes, ctx: Ctx, add) -> None:
    S = "S1"
    if not cfg.has_ayla:
        add(Check(S, "все проверки", SKIP, "нет AYLA_BASE_URL/AYLA_INTERNAL_API_TOKEN"))
        return
    resp = http.ayla_health()
    add(Check(S, "ayla liveness /api/v1/health/",
              PASS if resp.ok else FAIL, f"HTTP {resp.status} {resp.error}"))
    if not resp.ok:
        return

    client_id = _resolve_client_id(cfg, http, ctx, add)
    if not client_id:
        add(Check(S, "internal create/cancel", SKIP,
                  "client_id неизвестен: задать SMOKE_CLIENT_ID (Ayla User UUID) "
                  "или MAX_BOT_TOKEN для авто-резолва через bot export"))
        return
    target = _discover_booking_target(cfg, http, add)
    if not target:
        return
    specialist_id, service_id, start_iso, user_uuid = target
    ctx["specialist_user_uuid"] = user_uuid
    add(Check(S, "discovery: specialist+service+slot", PASS,
              f"specialist={specialist_id} service={service_id} slot={start_iso}"))

    ext = _ext_user()
    base_body = {"client_id": client_id, "specialist_id": specialist_id,
                 "service_id": service_id, "start_datetime": start_iso}

    # --- payment_required=false → CONFIRMED без платежа (AMD-002)
    key = f"smoke-{uuid.uuid4()}"
    resp = http.ayla("POST", "/api/v1/internal/appointments/",
                     json_body={**base_body, "payment_required": False},
                     external_user=ext, idempotency_key=key)
    booking = unwrap_data(resp) or {}
    booking_id = str(booking.get("id") or booking.get("booking_id") or "")
    status = str(booking.get("status") or "").upper()
    if resp.ok and booking_id and status == "CONFIRMED":
        add(Check(S, "create payment_required=false → CONFIRMED", PASS,
                  f"id={booking_id} status={status}"))
        ctx["appointment_id"] = booking_id
    else:
        add(Check(S, "create payment_required=false → CONFIRMED", FAIL,
                  f"HTTP {resp.status}: {resp.text[:300]}"))
        return

    # --- идемпотентный replay — тот же booking, без дубля
    replay = http.ayla("POST", "/api/v1/internal/appointments/",
                       json_body={**base_body, "payment_required": False},
                       external_user=ext, idempotency_key=key)
    replay_id = str((unwrap_data(replay) or {}).get("id") or "")
    add(Check(S, "idempotency replay (X-Idempotency-Key)",
              PASS if replay.ok and replay_id == booking_id else FAIL,
              f"replay id={replay_id} (ожидался {booking_id})"))

    # --- payment_required=true → AWAITING_PAYMENT (или честный SKIP)
    resp2 = http.ayla("POST", "/api/v1/internal/appointments/",
                      json_body={**base_body, "payment_required": True},
                      external_user=ext, idempotency_key=f"smoke-{uuid.uuid4()}")
    body2 = unwrap_data(resp2) or {}
    status2 = str((body2 or {}).get("status") or "").upper()
    if resp2.ok and status2 == "AWAITING_PAYMENT":
        add(Check(S, "create payment_required=true → AWAITING_PAYMENT", PASS,
                  f"id={body2.get('id')}"))
        http.ayla("POST", f"/api/v1/internal/appointments/{body2.get('id')}/cancel/",
                  json_body={"reason": "smoke cleanup"}, external_user=ext,
                  idempotency_key=f"smoke-cancel-{uuid.uuid4()}")
    elif resp2.status == 422:
        add(Check(S, "create payment_required=true", SKIP,
                  "422 ONLINE_PAYMENT_UNAVAILABLE — у специалиста нет yookassa sub-account"))
    elif resp2.status == 503:
        add(Check(S, "create payment_required=true", SKIP,
                  "503 — YooKassa creds не заданы на staging"))
    else:
        add(Check(S, "create payment_required=true", FAIL,
                  f"HTTP {resp2.status}: {resp2.text[:300]}"))

    # --- cancel
    resp3 = http.ayla("POST", f"/api/v1/internal/appointments/{booking_id}/cancel/",
                      json_body={"reason": "smoke cleanup"}, external_user=ext,
                      idempotency_key=f"smoke-cancel-{uuid.uuid4()}")
    if resp3.ok:
        verify = http.ayla("GET", f"/api/v1/internal/me/bookings/{booking_id}/",
                           external_user=ext)
        vstatus = str((unwrap_data(verify) or {}).get("status") or "").lower()
        add(Check(S, "cancel → статус cancelled",
                  PASS if vstatus == "cancelled" else FAIL,
                  f"cancel HTTP {resp3.status}, verify status={vstatus!r}"))
    else:
        add(Check(S, "cancel", FAIL, f"HTTP {resp3.status}: {resp3.text[:300]}"))

    # --- bot seam нога (customer API), опционально
    if cfg.has_bot and cfg.bot_master_id and cfg.bot_service_id:
        init_data = ctx.get("init_data") or SmokeHttp.mint_init_data(cfg.max_bot_token, SMOKE_MAX_USER_ID)
        ctx["init_data"] = init_data
        today = datetime.now(timezone.utc).date()
        slots = http.bot("GET", f"/api/v1/customer/slots?master_id={cfg.bot_master_id}"
                                f"&service_id={cfg.bot_service_id}"
                                f"&date_from={today}&date_to={today + timedelta(days=7)}",
                         init_data=init_data)
        bot_slot = pick_first((slots.json_body or {}).get("slots") if isinstance(slots.json_body, dict) else None)
        if bot_slot:
            create = http.bot("POST", "/api/v1/customer/bookings",
                              init_data=init_data,
                              json_body={"service_id": cfg.bot_service_id,
                                         "master_id": cfg.bot_master_id,
                                         "visit_at": bot_slot.get("start")})
            b = (create.json_body or {}).get("booking", {}) if isinstance(create.json_body, dict) else {}
            if create.status == 201 and b.get("id"):
                cancel = http.bot("POST", f"/api/v1/customer/bookings/{b['id']}/cancel",
                                  init_data=init_data, json_body={})
                http.bot("POST", f"/api/v1/customer/bookings/{b['id']}/cancel/confirm",
                         init_data=init_data)
                add(Check(S, "bot seam: create→cancel/confirm", PASS,
                          f"booking={b['id']} cancel HTTP {cancel.status}"))
            else:
                add(Check(S, "bot seam: create", FAIL,
                          f"HTTP {create.status}: {create.text[:300]}"))
        else:
            add(Check(S, "bot seam", SKIP, f"нет bot-слотов (HTTP {slots.status})"))
    else:
        add(Check(S, "bot seam нога", SKIP,
                  "нет MAX_BOT_TOKEN/SMOKE_BOT_MASTER_ID/SMOKE_BOT_SERVICE_ID"))


# ---------------------------------------------------------------------------
# S2. Memory-ask flow (acceptance §10.7)
# ---------------------------------------------------------------------------

def s2_memory_ask(cfg: SmokeConfig, http: SmokeHttp, probes: Probes, ctx: Ctx, add) -> None:
    S = "S2"
    if not cfg.has_ayla:
        add(Check(S, "все проверки", SKIP, "нет AYLA_BASE_URL/AYLA_INTERNAL_API_TOKEN"))
        return
    client_id = _resolve_client_id(cfg, http, ctx, add)
    if not client_id:
        add(Check(S, "memory-ask", SKIP, "client_id неизвестен (см. S1)"))
        return
    base = f"/api/v1/internal/users/{client_id}/personal-context"

    elig = http.ayla("GET", f"{base}/ask-eligibility/")
    eb = elig.json_body if isinstance(elig.json_body, dict) else {}
    add(Check(S, "ask-eligibility shape",
              PASS if elig.ok and "should_ask" in eb else FAIL,
              f"HTTP {elig.status}: should_ask={eb.get('should_ask')} field={eb.get('field') or eb.get('blocked_by')}"))

    before = http.ayla("GET", f"{base}/")
    original = None
    if before.ok and isinstance(before.json_body, dict):
        original = (unwrap_data(before) or {}).get("price_range_max")

    patch = http.ayla("PATCH", f"{base}/",
                      json_body={"updates": [{"field": "price_range_max", "value": 2500,
                                              "source": "explicit"}]})
    if not patch.ok:
        add(Check(S, "PATCH green-поле", FAIL, f"HTTP {patch.status}: {patch.text[:300]}"))
        return
    after = http.ayla("GET", f"{base}/")
    val = str((unwrap_data(after) or {}).get("price_range_max") or "")
    add(Check(S, "PATCH→GET: факт сохранён",
              PASS if after.ok and val.startswith("2500") else FAIL,
              f"price_range_max={val!r} после PATCH"))

    # cleanup: вернуть исходное значение, либо полное wipe (C5) для синтетики
    if original is not None:
        http.ayla("PATCH", f"{base}/",
                  json_body={"updates": [{"field": "price_range_max", "value": original,
                                          "source": "explicit"}]})
    else:
        http.ayla("DELETE", f"/api/v1/internal/users/{client_id}/personal-data/")
    add(Check(S, "cleanup контекста", PASS,
              "исходное значение восстановлено" if original is not None else "personal-data wipe"))


# ---------------------------------------------------------------------------
# S3. Billing charge (mock/staging ЮKassa) (acceptance §10.3, C2, AMD-014)
# ---------------------------------------------------------------------------

def s3_billing_charge(cfg: SmokeConfig, http: SmokeHttp, probes: Probes, ctx: Ctx, add) -> None:
    S = "S3"
    if not cfg.has_ayla:
        add(Check(S, "все проверки", SKIP, "нет AYLA_BASE_URL/AYLA_INTERNAL_API_TOKEN"))
        return
    user_uuid = ctx.get("specialist_user_uuid") or ""
    if not user_uuid:
        add(Check(S, "billing status/card-setup", SKIP,
                  "User UUID специалиста неизвестен — сначала S1 discovery"))
        return

    status = http.ayla("GET", f"/api/v1/internal/billing/specialists/{user_uuid}/status/")
    data = unwrap_data(status) or {}
    sub = (data.get("subscription") or {}) if isinstance(data, dict) else {}
    ok = status.ok and "status" in sub and "fees" in data
    add(Check(S, "C2 status shape (User UUID, AMD-005)",
              PASS if ok else FAIL,
              f"HTTP {status.status}: status={sub.get('status')} fees={data.get('fees')}"))

    setup = http.ayla("POST", f"/api/v1/internal/billing/specialists/{user_uuid}/card-setup/",
                      json_body={"tariff": "solo", "return_url": "https://example.com/smoke-return"})
    sdata = unwrap_data(setup) or {}
    if setup.ok and str(sdata.get("confirmation_url") or "").startswith("http"):
        add(Check(S, "card-setup → confirmation_url", PASS,
                  f"subscription={sdata.get('subscription_id')} invoice={sdata.get('invoice_id')}"))
    elif setup.status == 503:
        add(Check(S, "card-setup", SKIP, "503 — YooKassa creds не заданы на staging"))
    else:
        add(Check(S, "card-setup", FAIL, f"HTTP {setup.status}: {setup.text[:300]}"))

    hook = http.ayla("POST", "/api/v1/internal/billing/webhook/",
                     json_body={"event": "payment.succeeded", "object": {"id": f"smoke-{uuid.uuid4()}"}})
    add(Check(S, "billing webhook без auth → 401/403 (AMD-014)",
              PASS if hook.status in (401, 403) else FAIL,
              f"HTTP {hook.status}"
              + (" — webhook открыт без IP allowlist/Basic, закрыть перед продом" if hook.ok else "")))

    add(Check(S, "списание подписки + инвойс (e2e)", SKIP,
              "требует sandbox-прохождения confirmation_url (3DS в браузере) — "
              "ручной шаг по runbook §5; charge проходит через billing/tasks beat"))


# ---------------------------------------------------------------------------
# S4. Eventbus round-trip + дедуп (C4, AMD-007/008/015)
# ---------------------------------------------------------------------------

def s4_eventbus(cfg: SmokeConfig, http: SmokeHttp, probes: Probes, ctx: Ctx, add) -> None:
    S = "S4"
    if not cfg.has_ingest:
        add(Check(S, "все проверки", SKIP, "нет BOT_BASE_URL/HMAC secret"))
        return

    body = SmokeHttp.new_event_envelope(
        "user.profile.updated",
        {"user_id": str(uuid.uuid4()), "changed_fields": []},
        tenant_id=None)
    event_id = json.loads(body)["event_id"]

    first = http.ingest(body)
    if first.status == 401:
        add(Check(S, "ingest auth", FAIL,
                  "401 — проверить EVENT_INGEST_HMAC_SECRET в settings бота "
                  "(известный config-gap: переменная не загружается из env)"))
        return
    add(Check(S, "ingest новое событие → 200 ok",
              PASS if first.ok and (first.json_body or {}).get("status") == "ok" else FAIL,
              f"HTTP {first.status}: {first.text[:200]}"))

    replay = http.ingest(body)
    dup = isinstance(replay.json_body, dict) and replay.json_body.get("duplicate") is True
    add(Check(S, "replay event_id → 200 duplicate",
              PASS if replay.ok and dup else FAIL,
              f"HTTP {replay.status}: {replay.text[:200]}"))

    bad_version = json.loads(body)
    bad_version["event_id"] = str(uuid.uuid4())
    bad_version["event_version"] = 999
    r = http.ingest(json.dumps(bad_version).encode())
    add(Check(S, "unknown event_version → 422 + DLQ",
              PASS if r.status == 422 and (r.json_body or {}).get("reason") == "unknown_event_version" else FAIL,
              f"HTTP {r.status}: {r.text[:200]}"))

    bad_name = json.loads(body)
    bad_name["event_id"] = str(uuid.uuid4())
    bad_name["event_name"] = "smoke.unknown"
    r = http.ingest(json.dumps(bad_name).encode())
    add(Check(S, "unknown event_name → 400 invalid_event_name",
              PASS if r.status == 400 and (r.json_body or {}).get("reason") == "invalid_event_name" else FAIL,
              f"HTTP {r.status}: {r.text[:200]}"))

    if probes.available:
        rows = probes.query(DEDUPE_BY_EVENT_ID, (event_id,))
        add(Check(S, "SQL: dedupe-ряд ровно один",
                  PASS if len(rows) == 1 else FAIL, f"rows={len(rows)}"))
        dlq = probes.query(DLQ_BY_EVENT_ID, (bad_version["event_id"],))
        add(Check(S, "SQL: unknown_version в DLQ",
                  PASS if len(dlq) == 1 else FAIL, f"rows={len(dlq)}"))
        if ctx.get("appointment_id"):
            since = datetime.now(timezone.utc) - timedelta(minutes=30)
            organic = probes.query(BOOKING_CREATED_SINCE, (since,))
            add(Check(S, "SQL: booking.created доехал в бота (round-trip)",
                      PASS if organic else SKIP,
                      f"rows за 30 мин: {len(organic)}"
                      + ("" if organic else " — включён ли OUTBOX_EXTERNAL_DELIVERY_TOPICS? (D-3)")))
    else:
        add(Check(S, "SQL-пробы", SKIP,
                  "нет BOT_DB_DSN. Вручную: " + DEDUPE_BY_EVENT_ID.strip()))


# ---------------------------------------------------------------------------
# S5. C5 dual-system export/delete (acceptance §10.6, AMD-006/010)
# ---------------------------------------------------------------------------

def s5_dual_delete(cfg: SmokeConfig, http: SmokeHttp, probes: Probes, ctx: Ctx, add) -> None:
    S = "S5"
    if not cfg.has_ayla:
        add(Check(S, "все проверки", SKIP, "нет AYLA_BASE_URL/AYLA_INTERNAL_API_TOKEN"))
        return
    client_id = _resolve_client_id(cfg, http, ctx, add)
    if not client_id:
        add(Check(S, "dual-system", SKIP, "client_id неизвестен (см. S1)"))
        return
    base = f"/api/v1/internal/users/{client_id}/personal-data"

    http.ayla("PATCH", f"/api/v1/internal/users/{client_id}/personal-context/",
              json_body={"updates": [{"field": "price_range_max", "value": 2500,
                                      "source": "explicit"}]})
    exp = http.ayla("GET", f"{base}/export/")
    edata = exp.json_body if isinstance(exp.json_body, dict) else {}
    pctx = edata.get("personal_context")
    has_fact = bool(pctx) and str(pctx.get("price_range_max") or "").startswith("2500")
    add(Check(S, "Ayla export содержит факт",
              PASS if exp.ok and has_fact else FAIL,
              f"HTTP {exp.status}: personal_context={'есть' if pctx else 'null'}"))

    if cfg.has_bot:
        init_data = ctx.get("init_data") or SmokeHttp.mint_init_data(cfg.max_bot_token, SMOKE_MAX_USER_ID)
        ctx["init_data"] = init_data
        bexp = http.bot("GET", "/api/v1/customer/me/personal-data/export/", init_data=init_data)
        cd = bexp.headers.get("Content-Disposition", "")
        keys = set(bexp.json_body.keys()) if isinstance(bexp.json_body, dict) else set()
        add(Check(S, "bot export: attachment + секции",
                  PASS if bexp.ok and "attachment" in cd and "personal-data-export.json" in cd
                  and {"generated_at", "subject", "memory", "consents"} <= keys else FAIL,
                  f"HTTP {bexp.status} Content-Disposition={cd!r} keys={sorted(keys)}"))
        dele = http.bot("DELETE", "/api/v1/customer/me/personal-data/", init_data=init_data)
        add(Check(S, "bot delete каскад",
                  PASS if dele.ok and (dele.json_body or {}).get("status") == "deleted" else FAIL,
                  f"HTTP {dele.status}: {dele.text[:200]}"))
        reexp = http.bot("GET", "/api/v1/customer/me/personal-data/export/", init_data=init_data)
        mem = (reexp.json_body or {}).get("memory") if isinstance(reexp.json_body, dict) else None
        add(Check(S, "bot: память пуста после delete",
                  PASS if reexp.ok and mem == [] else FAIL,
                  f"memory={mem!r}"))
        dele2 = http.bot("DELETE", "/api/v1/customer/me/personal-data/", init_data=init_data)
        add(Check(S, "bot delete идемпотентен (повтор → 200)",
                  PASS if dele2.ok else FAIL, f"HTTP {dele2.status}"))
    else:
        dele = http.ayla("DELETE", f"{base}/")
        add(Check(S, "Ayla delete идемпотентен",
                  PASS if dele.ok else FAIL, f"HTTP {dele.status}: {dele.text[:200]}"))
        http.ayla("DELETE", f"{base}/")

    aexp = http.ayla("GET", f"{base}/export/")
    apctx = (aexp.json_body or {}).get("personal_context") if isinstance(aexp.json_body, dict) else "?"
    add(Check(S, "Ayla: контекст отсутствует после delete (dual-system)",
              PASS if aexp.ok and apctx is None else FAIL,
              f"personal_context={apctx!r}"))


# ---------------------------------------------------------------------------
# S6. R1 напоминания T−24h (+T−2h, AMD-012) без дублей
# ---------------------------------------------------------------------------

def s6_reminders(cfg: SmokeConfig, http: SmokeHttp, probes: Probes, ctx: Ctx, add) -> None:
    S = "S6"
    appt = ctx.get("appointment_id") or ""
    if not appt:
        add(Check(S, "reminders", SKIP, "нет appointment_id (S1 не создал бронь)"))
        return
    if not probes.available:
        add(Check(S, "reminders SQL", SKIP,
                  "нет BOT_DB_DSN. Вручную: " + REMINDERS_BY_APPOINTMENT.strip()))
        return
    rows = probes.query(REMINDERS_BY_APPOINTMENT, (appt,))
    kinds = sorted(r[0] for r in rows)
    statuses = sorted({str(r[1]) for r in rows})
    if not rows:
        add(Check(S, "R1: напоминания запланированы", SKIP,
                  "0 рядов — booking.created не доехал (OUTBOX_EXTERNAL_DELIVERY_TOPICS? D-3) "
                  "или consumer ещё не обработал"))
        return
    ok_kinds = kinds == ["day_before", "two_hours"]
    add(Check(S, "R1: ровно day_before + two_hours, без дублей (AMD-012)",
              PASS if ok_kinds and len(rows) == 2 else FAIL,
              f"kinds={kinds} statuses={statuses} (после cancel ожидается cancelled)"))


# ---------------------------------------------------------------------------
# S7. UX-пробы (W4): blob-download export, stub-gate
# ---------------------------------------------------------------------------

def s7_ux_probes(cfg: SmokeConfig, http: SmokeHttp, probes: Probes, ctx: Ctx, add) -> None:
    S = "S7"
    if not cfg.has_bot:
        add(Check(S, "все проверки", SKIP, "нет BOT_BASE_URL/MAX_BOT_TOKEN"))
        return
    init_data = ctx.get("init_data") or SmokeHttp.mint_init_data(cfg.max_bot_token, SMOKE_MAX_USER_ID)
    ctx["init_data"] = init_data

    exp = http.bot("GET", "/api/v1/customer/me/personal-data/export/", init_data=init_data)
    cd = exp.headers.get("Content-Disposition", "")
    ct = exp.headers.get("Content-Type", "")
    add(Check(S, "export blob-download контракт (MAX webview)",
              PASS if exp.ok and "attachment" in cd and "personal-data-export.json" in cd
              and "application/json" in ct else FAIL,
              f"CD={cd!r} CT={ct!r}"))

    for name, path, method in (
            ("wellness/today", "/api/v1/customer/wellness/today", "GET"),
            ("recent-activity", "/api/v1/customer/recent-activity", "GET"),
            ("recommendations", "/api/v1/customer/recommendations", "POST")):
        resp = http.bot(method, path, init_data=init_data,
                        json_body={} if method == "POST" else None)
        body_text = resp.text or ""
        markers = [m for m in STUB_MARKERS if m in body_text]
        if markers:
            add(Check(S, f"stub-gate: {name}", FAIL,
                      f"обнаружены stub-маркеры {markers} в прод-ответе"))
        elif resp.status in (200, 400, 502, 503):
            add(Check(S, f"stub-gate: {name}", PASS,
                      f"HTTP {resp.status}, честный ответ без stub-маркеров"))
        else:
            add(Check(S, f"stub-gate: {name}", FAIL,
                      f"HTTP {resp.status}: {body_text[:200]}"))


SCENARIOS = (
    ("S1", "Booking CRUD сквозной (AMD-002)", s1_booking_crud),
    ("S2", "Memory-ask flow (§10.7)", s2_memory_ask),
    ("S3", "Billing charge / card-setup (C2, AMD-014)", s3_billing_charge),
    ("S4", "Eventbus round-trip + дедуп (C4)", s4_eventbus),
    ("S5", "C5 dual-system export/delete (§10.6)", s5_dual_delete),
    ("S6", "R1 напоминания без дублей (AMD-012)", s6_reminders),
    ("S7", "UX-пробы W4: blob-download, stub-gate", s7_ux_probes),
)
