"""Адрес, который видит клиент, — адрес места, не человека (§9 / L6, L8a; DRF-1687).

§9: «все старые адреса перестают быть авторитетными». До L6 адрес мастера
уходил клиенту четырьмя дорогами: карточка услуги, карточка/список
мастеров, поиск, DTO движка подбора — и пятой, самой громкой:
push-напоминанием за час до визита (``notifications/tasks.py``), где
неверный адрес отправляет человека не туда.

L8a добил хвост, которого сторож L6 не видел, потому что держал список из
двух файлов, а не класс: карточка подтверждения записи у LLM
(``ai/tools_handlers.py``), общий контекст уведомлений о записи
(``notifications/outbox_handlers.py``), фильтр города движка подбора
(``address__icontains`` по адресу человека) и демо-сид, писавший адрес
салона в профиль каждого мастера.

Сторожа:

* **``offer_address``** — единственный источник адреса для показа: пусто
  без места и при неподтверждённом месте; адрес ПОДТВЕРЖДЁННОГО места
  показывается и без геокода;
* **поведение** — напоминание, карточка подтверждения и контекст
  уведомлений несут адрес места; фильтр города смотрит на город места;
  старый адрес профиля заполнен в тестах НАРОЧНО и нигде не всплывает;
* **перепись** (L8a) — все обращения к именам колонок ``address`` /
  ``location_lat`` / ``location_lng`` во всём рабочем коде, по файлам и с
  числом. Новое обращение где угодно — красное, пока его не классифицируют:
  старая колонка профиля (запрещено, кроме стирания и замера) или место /
  салон / DTO / координата клиента (вписать с основанием). Это сторож класса,
  а не сегодняшнего написания: имя переменной (``sp``, ``s``, ``obj``) ему
  безразлично.

Названный предел переписи: строки на уровне класса (``Meta.fields``,
``readonly_fields``, ``source="..."``), динамические имена в ``getattr`` и
сырой SQL она не видит. Координаты в этих формах держит сторож 1687
(``ALLOWED_READERS``), имена на проводе — ``tenants.wire`` и
``test_wire_names_are_declared_place_fields_not_model_columns``.
"""
from __future__ import annotations

import ast
import datetime as dt
from collections import Counter
from pathlib import Path

import pytest
from django.utils import timezone

from tenants.distance import offer_address
from tenants.models import GeocodeStatus, LocationStatus, ServiceLocation, Tenant
from users.models import SpecialistProfile, User

ROOT = Path(__file__).resolve().parents[2]

#: Модули, где адрес мастера уходит наружу и обязан идти через ``offer_address``.
ADDRESS_EMITTERS = (
    "ai/application/services/recommendation_engine.py",
    "notifications/tasks.py",
    "notifications/outbox_handlers.py",
)


def test_emitters_take_the_address_from_the_place_not_the_profile():
    for rel in ADDRESS_EMITTERS:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("offer_address")
        ]
        reads = [
            ast.unparse(n) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "address"
        ]
        assert calls, f"{rel}: адрес не берётся через offer_address"
        assert not reads, f"{rel}: чтение .address мимо места: {reads}"


# ---------------------------------------------------------------------------
# Перепись обращений к именам старых колонок (L8a)
# ---------------------------------------------------------------------------

COLUMN_NAMES = frozenset({"address", "location_lat", "location_lng"})
_LIST_KWARGS = frozenset({"update_fields", "fields", "only", "defer"})
_LIST_CALLS = frozenset({"values", "values_list", "only", "defer", "order_by"})
_SKIP_PARTS = frozenset({"tests", "migrations", "venv", ".venv", "node_modules"})

#: Файл → (число обращений, чьё это поле). Число точное: рост — новое
#: обращение, которое надо классифицировать; убыль — перепись врёт и её
#: надо опустить, иначе следующее обращение спрячется в освободившейся квоте.
USE_REGISTRY: dict[str, tuple[int, str]] = {
    # --- старые колонки ПРОФИЛЯ мастера: законны только до L8b (DRF-1892) ---
    "users/deletion_executor.py": (9, "профиль: стирание при удалении аккаунта (§7 D3) и проверка остатка"),
    "users/personal_data_api.py": (
        4, "профиль и своё место: выгрузка субъекту по 152-ФЗ (C5.1, DRF-1918) — только чтение, не показ"
    ),
    "core/management/commands/surface_state.py": (4, "профиль и Tenant: замер состояния — счёт, не показ"),
    "tenants/management/commands/propose_master_locations.py": (
        8, "профиль, Tenant и место: триаж старого адреса (§9, H4, DRF-1924) — печать; адреса с --with-address"
    ),
    # --- НЕ профиль: место, салон, DTO кандидата, координата клиента ---
    "ai/application/services/recommendation_engine.py": (1, "ScoredSpecialist(address=offer_address(s)) — DTO"),
    "ai/application/services/specialist_context_builder.py": (2, "SpecialistCandidate ← ScoredSpecialist — DTO"),
    "ai/tools_handlers.py": (1, "c.address — кандидат движка (DTO), адрес уже от места"),
    "ai/views.py": (3, "контекст чата: координата КЛИЕНТА (§8)"),
    "core/geocoding/apply.py": (1, "ServiceLocation.address — геокодер места"),
    "services/management/commands/seed_demo_salons.py": (2, "Tenant.address — адрес салона в демо-сиде"),
    "tenants/distance.py": (1, "place.address — сам offer_address"),
    "tenants/management/commands/geocode_locations.py": (5, "ServiceLocation.address"),
    "tenants/management/commands/promote_tenant_location.py": (3, "Tenant.address → ServiceLocation(address=)"),
    "tenants/service_location.py": (2, "ServiceLocation.address (self)"),
    "users/home_api.py": (1, "s.address — кандидат движка (DTO)"),
    "users/internal_catalog_api.py": (1, "getattr(obj.tenant, 'address') — адрес салона"),
}

#: Нижняя граница переписи: на 7374b07d рабочих .py-файлов 427. Меньше —
#: скан не того корня, и «лишних обращений нет» значило бы «не искал».
MIN_SCANNED = 400


def _base(name: str) -> str:
    return name.split("__")[0]


def _module_string_lists(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    """Модульные константы-перечни строк: ``X = ("a", "b")`` / ``X: tuple[...] = (...)``.

    DRF-1918: исполнитель удаления сохраняет ``update_fields=[*SPECIALIST_PROFILE_ERASED_FIELDS, ...]``
    — перепись обязана видеть в этом те же литералы, что и в прежнем списке, иначе
    стирание старых колонок пропадает из счёта. Названный предел: звёздочка по имени,
    которое не является такой константой того же модуля, не раскрывается.
    """
    found: dict[str, tuple[str, ...]] = {}
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            name, value = stmt.target.id, stmt.value
        else:
            continue
        if isinstance(value, (ast.Tuple, ast.List)) and value.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
        ):
            found[name] = tuple(e.value for e in value.elts)
    return found


def _list_strings(node: ast.AST, lists: dict[str, tuple[str, ...]]):
    for e in node.elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            yield e.value
        elif isinstance(e, ast.Starred) and isinstance(e.value, ast.Name) and e.value.id in lists:
            yield from lists[e.value.id]


def _column_uses(tree: ast.AST) -> list[str]:
    uses: list[str] = []
    lists = _module_string_lists(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in COLUMN_NAMES:
            uses.append(f"{n.lineno}: {ast.unparse(n)}")
        elif isinstance(n, ast.Call):
            fn = ast.unparse(n.func)
            short = fn.split(".")[-1]
            if (
                fn in ("getattr", "setattr", "hasattr") and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant) and n.args[1].value in COLUMN_NAMES
            ):
                uses.append(f"{n.lineno}: {ast.unparse(n)[:80]}")
            for kw in n.keywords:
                if kw.arg and _base(kw.arg) in COLUMN_NAMES:
                    uses.append(f"{n.lineno}: {short}({kw.arg}=)")
                if kw.arg in _LIST_KWARGS and isinstance(kw.value, (ast.List, ast.Tuple)):
                    uses.extend(
                        f"{n.lineno}: {kw.arg}[{v}]" for v in _list_strings(kw.value, lists)
                        if _base(v) in COLUMN_NAMES
                    )
            if short in _LIST_CALLS:
                uses.extend(
                    f"{n.lineno}: {short}({a.value})" for a in n.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and _base(a.value) in COLUMN_NAMES
                )
    return uses


def _census() -> tuple[int, dict[str, list[str]]]:
    scanned, found = 0, {}
    for path in sorted(ROOT.rglob("*.py")):
        rel_parts = path.relative_to(ROOT).parts
        if any(p in _SKIP_PARTS or p.startswith(".") for p in rel_parts):
            continue
        scanned += 1
        uses = _column_uses(ast.parse(path.read_text(encoding="utf-8")))
        if uses:
            found["/".join(rel_parts)] = uses
    return scanned, found


def test_every_use_of_the_old_column_names_is_classified():
    scanned, found = _census()
    assert scanned >= MIN_SCANNED, f"просканировано {scanned} файлов — корень не тот"

    actual = Counter({rel: len(uses) for rel, uses in found.items()})
    expected = Counter({rel: n for rel, (n, _) in USE_REGISTRY.items()})
    if actual != expected:
        lines = []
        for rel in sorted(set(actual) | set(expected)):
            if actual[rel] != expected[rel]:
                lines.append(f"{rel}: в переписи {expected[rel]}, в коде {actual[rel]}")
                lines.extend(f"    {u}" for u in found.get(rel, []))
        pytest.fail(
            "обращения к address/location_lat/location_lng разошлись с переписью (§9, L8a).\n"
            "Старая колонка профиля мастера — только стирание и замер; адрес для показа — offer_address().\n"
            "Место/салон/DTO/координата клиента — вписать в USE_REGISTRY с основанием.\n  "
            + "\n  ".join(lines)
        )


def test_census_sees_the_profile_erasure_it_allows():
    """Положительный контроль: без него «расхождений нет» = «сканер слеп»."""
    _, found = _census()
    erasure = found.get("users/deletion_executor.py", [])
    assert any(u.endswith(": sp.address") for u in erasure), erasure
    assert any("update_fields[location_lat]" in u for u in erasure), erasure


# ---------------------------------------------------------------------------
# Поведение
# ---------------------------------------------------------------------------

def _master(phone: str, tenant=None) -> SpecialistProfile:
    u = User.objects.create_user(username=f"a{phone[-4:]}", password="x", role="specialist", phone=phone)
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    # старый адрес профиля заполнен НАРОЧНО: его не должно быть видно
    p.address = "СТАРЫЙ адрес профиля, ул. Человека, 1"
    p.save(update_fields=["tenant", "address"])
    return p


def _place(tenant, address, *, confirmed: bool, geocoded: bool, confirmed_by=None, city="Пенза") -> ServiceLocation:
    kwargs = dict(tenant=tenant, address=address, city=city, geocode_provider="t")
    if geocoded:
        kwargs.update(geocode_status=GeocodeStatus.OK, latitude="53.195878", longitude="45.018316")
    if confirmed:
        kwargs.update(
            status=LocationStatus.CONFIRMED, confirmed_by=confirmed_by,
            confirmed_at=timezone.now(), confirmed_source_ref="§10",
        )
    return ServiceLocation.objects.create(**kwargs)


def _appointment(m: SpecialistProfile, client_phone: str):
    from appointments.models import Appointment
    from notifications.tasks import REMINDER_LEAD_MINUTES
    from services.models import Service, ServiceCategory

    client = User.objects.create_user(
        username=f"cl{client_phone[-4:]}", password="x", role="client", phone=client_phone,
    )
    cat, _ = ServiceCategory.objects.get_or_create(name="cat-l6", slug="cat-l6")
    svc = Service.objects.create(specialist=m, category=cat, name="Маникюр", price=1000, duration_minutes=60)
    start = timezone.now() + dt.timedelta(minutes=REMINDER_LEAD_MINUTES)
    return Appointment.objects.create(
        client=client, specialist=m, service=svc, start_datetime=start,
        end_datetime=start + dt.timedelta(minutes=60), status=Appointment.Status.CONFIRMED, price=svc.price,
    )


@pytest.mark.django_db
def test_offer_address_is_the_confirmed_place_or_nothing():
    tenant = Tenant.objects.create(slug="addr", name="A")
    op = User.objects.create_user(username="op-a", password="x", phone="+79990001800")
    m = _master("+79990001801", tenant)

    assert offer_address(m) == ""                                  # места нет — старый адрес не подставляется

    m.works_at = _place(tenant, "ул. Не подтверждённая, 2", confirmed=False, geocoded=True)
    assert offer_address(m) == ""                                  # REVIEW_REQUIRED — клиенту не называем

    m.works_at = _place(tenant, "ул. Подтверждённая, 3", confirmed=True, geocoded=False, confirmed_by=op)
    assert offer_address(m) == "ул. Подтверждённая, 3"             # подтверждено, ещё без геокода — адрес есть

    m.works_at = _place(tenant, "ул. Полная, 4", confirmed=True, geocoded=True, confirmed_by=op)
    assert offer_address(m) == "ул. Полная, 4"

    inactive = _place(tenant, "ул. Закрытая, 5", confirmed=True, geocoded=True, confirmed_by=op)
    ServiceLocation.objects.filter(pk=inactive.pk).update(status=LocationStatus.INACTIVE)
    inactive.refresh_from_db()
    m.works_at = inactive
    assert offer_address(m) == ""                                  # закрытое место — не адрес


@pytest.mark.django_db
def test_reminder_push_carries_the_place_address_not_the_profile_address():
    from notifications.models import Notification
    from notifications.tasks import REMINDER_TEMPLATE_ID, dispatch_appointment_reminders

    tenant = Tenant.objects.create(slug="push", name="P")
    op = User.objects.create_user(username="op-p", password="x", phone="+79990001802")
    m = _master("+79990001803", tenant)
    m.display_name = "Мастер"
    m.status = SpecialistProfile.ProfileStatus.ACTIVE
    m.works_at = _place(tenant, "ул. Места, 7", confirmed=True, geocoded=False, confirmed_by=op)
    m.save(update_fields=["display_name", "status", "works_at"])
    appt = _appointment(m, "+79990001804")

    assert dispatch_appointment_reminders()["queued"] == 1
    row = Notification.objects.get(user=appt.client, template_id=REMINDER_TEMPLATE_ID)
    assert row.data["address"] == "ул. Места, 7"
    assert "СТАРЫЙ" not in row.body and "Человека" not in row.body


@pytest.mark.django_db
def test_booking_notification_context_carries_the_place_address():
    """Контекст шести уведомлений о записи (L8a): адрес места или пусто."""
    from appointments.models import Appointment
    from notifications.outbox_handlers import _appointment_context

    tenant = Tenant.objects.create(slug="outbox", name="O")
    op = User.objects.create_user(username="op-o", password="x", phone="+79990001810")
    m = _master("+79990001811", tenant)
    appt = _appointment(m, "+79990001812")

    assert _appointment_context(appt)["address"] == ""             # места нет — старый адрес не подставляется

    m.works_at = _place(tenant, "ул. Места, 9", confirmed=True, geocoded=False, confirmed_by=op)
    m.save(update_fields=["works_at"])
    appt = Appointment.objects.select_related("specialist__works_at").get(pk=appt.pk)
    ctx = _appointment_context(appt)
    assert ctx["address"] == "ул. Места, 9"
    assert "СТАРЫЙ" not in repr(ctx)


@pytest.mark.django_db
def test_confirm_booking_card_carries_the_place_address_not_the_profile_address():
    """Карточка подтверждения записи у LLM (L8a): модель вправе озвучить поле."""
    from ai.tools import ActionType
    from ai.tools_handlers import handle_confirm_booking

    tenant = Tenant.objects.create(slug="card", name="C")
    op = User.objects.create_user(username="op-c", password="x", phone="+79990001820")
    m = _master("+79990001821", tenant)
    appt = _appointment(m, "+79990001822")
    args = {"specialist_id": str(m.id), "service_id": str(appt.service_id), "datetime": "2026-10-01T14:00:00+03:00"}

    card = handle_confirm_booking(args)
    assert card.action_type == ActionType.CONFIRM_BOOKING, card.action_data
    assert card.action_data["address"] == ""                       # места нет — старый адрес не подставляется

    m.works_at = _place(tenant, "ул. Места, 8", confirmed=True, geocoded=False, confirmed_by=op)
    m.save(update_fields=["works_at"])
    card = handle_confirm_booking(args)
    assert card.action_data["address"] == "ул. Места, 8"
    assert "СТАРЫЙ" not in repr(card.action_data)


@pytest.mark.django_db
def test_engine_city_filter_reads_the_place_or_salon_city_not_the_profile_address():
    """Фильтр города движка (L8a): место → салон → не попадает; адрес человека не читается.

    Старые адреса профилей нарочно «перевёрнуты» относительно мест и салонов:
    прежний ``address__icontains`` пустил бы ровно тех, кого новый фильтр не
    пускает, и не пустил бы тех, кого пускает.
    """
    from ai.application.services.recommendation_engine import RecommendationEngine, RecommendationQuery

    penza_salon = Tenant.objects.create(slug="city-penza", name="Пенза-салон", city="Пенза")
    moscow_salon = Tenant.objects.create(slug="city-moscow", name="Москва-салон", city="Москва")
    no_city_salon = Tenant.objects.create(slug="city-none", name="Салон без города")
    op = User.objects.create_user(username="op-g", password="x", phone="+79990001830")

    def active(phone: str, tenant, old_address: str, place: ServiceLocation | None) -> SpecialistProfile:
        m = _master(phone, tenant)
        m.address = old_address
        m.status = SpecialistProfile.ProfileStatus.ACTIVE
        m.is_available = True
        m.is_booking_enabled = True
        m.works_at = place
        m.save()
        return m

    # место не подтверждено или его нет — говорит город салона
    penza_salon_no_place = active("+79990001831", penza_salon, "СТАРЫЙ, Москва, ул. Человека, 1", None)
    penza_salon_unconfirmed_moscow = active(
        "+79990001834", penza_salon, "СТАРЫЙ, Москва, ул. Человека, 4",
        _place(penza_salon, "ул. Арбат, 2", confirmed=False, geocoded=False, city="Москва"),
    )
    moscow_salon_no_place = active("+79990001835", moscow_salon, "СТАРЫЙ, Пенза, ул. Человека, 5", None)
    # никто не знает города — в городской ответ не попадает (как было и как у Tenant.city)
    unknown_city = active("+79990001836", no_city_salon, "СТАРЫЙ, Пенза, ул. Человека, 6", None)
    # подтверждённое место говорит первым, даже против города салона
    moscow_salon_place_in_penza = active(
        "+79990001832", moscow_salon, "СТАРЫЙ, Москва, ул. Человека, 2",
        _place(moscow_salon, "ул. Пушкина, 45", confirmed=True, geocoded=False, confirmed_by=op),
    )
    penza_salon_place_in_moscow = active(
        "+79990001833", penza_salon, "СТАРЫЙ, Пенза, ул. Человека, 3",
        _place(penza_salon, "ул. Тверская, 1", confirmed=True, geocoded=False, confirmed_by=op, city="Москва"),
    )

    got = {
        p.id for p in RecommendationEngine()._fetch_candidates(
            RecommendationQuery(city="Пенза"), min_rating=0, limit=10,
        )
    }
    assert penza_salon_no_place.id in got
    assert penza_salon_unconfirmed_moscow.id in got        # неподтверждённое место не авторитетно (L6)
    assert moscow_salon_place_in_penza.id in got
    assert moscow_salon_no_place.id not in got
    assert penza_salon_place_in_moscow.id not in got
    assert unknown_city.id not in got                      # «пензенский» адрес человека не пускает
