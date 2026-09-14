"""DRF-1617 / B-2.1 — внутренний токен перестаёт означать право на любого субъекта.

Дефект: держатель общего внутреннего Bearer мог экспортировать, переписать
и стереть персданные ЛЮБОГО субъекта, поставив его UUID в URL. Токен
доказывал, что вызов пришёл от своего сервиса, и проверка на этом кончалась.

Разбор и форма тестов — из #318 (10.09, ayla-privacy), перенесены на
сегодняшний dev как свой PR; там же объяснено, почему проверка не должна
создавать строк (``resolve_external_user`` делает ``get_or_create`` — и
авторизующая проверка стала бы способом чеканить аккаунты подбором
заголовка).

### Что здесь настоящий отрицательный тест, а что только похож на него

«Неверный токен → отказ» проходил и ДО этой правки: периметр всегда отказывал
незнакомому Bearer. Он оставлен (:class:`TestPreExistingPerimeter`) и назван,
чтобы никто не добавил его снова как доказательство. Он доказывает дверь,
а не границу внутри неё.

Настоящие три: верный токен, называющий ЧУЖОГО субъекта; учётные данные,
выданные для другой цели (провижининг); экспорт **и** удаление чужого —
отдельно, потому что второе разрушительно и проверяется по эффекту.

### Что изменилось против #318

Флага «считать, не отказывать» нет. Бот называет субъект на всей этой
поверхности с ai-bot-platform#1535 (слит 11.09.2026), ``pilot_smoke``
научен в этом же PR — незнакомый зовущий отказывается сразу. Журнала аудита
(§96) здесь тоже нет: отдельный предмет, отдельный PR.

### Поверхность выросла: DeletionRequest

#374 добавил ``…/deletion-requests/`` — заявку на удаление аккаунта, тоже по
субъекту из URL. Заявка на удаление чужого человека — не чтение и не
запись, а третий класс: запуск процедуры. Она в списке.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.urls import resolve
from rest_framework.test import APIClient

from users.models import User, UserPersonalContext
from users.permissions import IsInternalBearerForSubject

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token"  # noqa: S105
PROVISIONING_TOKEN = "test-provisioning-only-token"  # noqa: S105


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING_TOKEN
    return settings


def _client(*, bearer: str | None = RUNTIME_TOKEN, actor: str | None = None) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _subject(nick: str, external_id: str) -> tuple[User, str]:
    """Настоящий клиентский аккаунт плюс привязанная к нему внешняя личность.

    Повторяет то, что даёт ``bind_external_identity`` в бою: proxy-строка с
    внешним id, указывающая на живой аккаунт. Собрано руками, а не через
    сервис привязки, чтобы смена ПОЛИТИКИ привязки не меняла молча то, что
    этот тест считает владельцем.
    """
    real = User.objects.create_user(
        username=nick, password="x", role="client", phone=f"+7999000{nick[-4:]}",
    )
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False,
        linked_user=real,
    )
    return real, external_id


@pytest.fixture
def alice() -> tuple[User, str]:
    return _subject("alice1001", "bot:telegram:1001")


@pytest.fixture
def bob() -> tuple[User, str]:
    return _subject("bob2002", "bot:telegram:2002")


# Каждый маршрут охраняемой поверхности: (метод, шаблон). ``{subject}`` —
# субъект, названный зовущим; то, чему эта правка перестаёт верить.
EXPORT = ("get", "/api/v1/internal/users/{subject}/personal-data/export/")
DELETE = ("delete", "/api/v1/internal/users/{subject}/personal-data/")
CTX_GET = ("get", "/api/v1/internal/users/{subject}/personal-context/")
CTX_PATCH = ("patch", "/api/v1/internal/users/{subject}/personal-context/")
CTX_DELETE = ("delete", "/api/v1/internal/users/{subject}/personal-context/")
CTX_ELIG = ("get", "/api/v1/internal/users/{subject}/personal-context/ask-eligibility/")
CTX_ASKED = ("post", "/api/v1/internal/users/{subject}/personal-context/mark-asked/")
CTX_SKIP = ("post", "/api/v1/internal/users/{subject}/personal-context/skip/")
DEL_REQ_GET = ("get", "/api/v1/internal/users/{subject}/deletion-requests/")
DEL_REQ_POST = ("post", "/api/v1/internal/users/{subject}/deletion-requests/")
# DRF-1709 (12.09.2026): последняя дыра B-2.1 закрыта — карточка под субъектом.
PROFILE = ("get", "/api/v1/internal/users/{subject}/")

ALL_ROUTES = [
    EXPORT, DELETE, CTX_GET, CTX_PATCH, CTX_DELETE, CTX_ELIG, CTX_ASKED, CTX_SKIP,
    DEL_REQ_GET, DEL_REQ_POST, PROFILE,
]

_BODIES = {
    CTX_PATCH: {"updates": [{"field": "workplace_district", "value": "Центр"}]},
    CTX_ASKED: {"field": "preferred_time_slots"},
    CTX_SKIP: {"field": "preferred_time_slots"},
    DEL_REQ_POST: {"initiator": "bot"},
}


def _call(client: APIClient, route, subject_id):
    method, template = route
    url = template.format(subject=subject_id)
    body = _BODIES.get(route)
    if body is None:
        return getattr(client, method)(url)
    return getattr(client, method)(url, body, format="json")


def _ids(r):
    return f"{r[0]}:{r[1]}"


# ---------------------------------------------------------------------------
# Три отрицательных, о которых эта правка
# ---------------------------------------------------------------------------


class TestForeignSubjectDenied:
    """Верный токен, зовущий назвал себя — и назвал кого-то другого."""

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_every_route_refuses_a_foreign_subject(self, route, alice, bob):
        _, alice_id = alice
        bob_user, _ = bob
        resp = _call(_client(actor=alice_id), route, bob_user.pk)
        assert resp.status_code == 403, f"{route} пустил Алису к Бобу: {resp.status_code}"
        assert "does not match" in resp.json()["error"]["message"]

    def test_export_of_a_foreign_subject_returns_nothing(self, alice, bob):
        """Отказ ДО чтения, а не фильтр после."""
        _, alice_id = alice
        bob_user, _ = bob
        bob_user.email = "bob@example.com"
        bob_user.save(update_fields=["email"])
        resp = _call(_client(actor=alice_id), EXPORT, bob_user.pk)
        assert resp.status_code == 403
        assert "bob@example.com" not in resp.content.decode()

    def test_delete_of_a_foreign_subject_leaves_the_data_intact(self, alice, bob):
        """Разрушительный глагол — по эффекту, а не по статусу: 403, который
        всё равно стёр, — худший исход и самый незаметный."""
        _, alice_id = alice
        bob_user, _ = bob
        UserPersonalContext.objects.create(
            user=bob_user, workplace_district="Заводской",
            data_sources={"workplace_district": "explicit"},
        )
        resp = _call(_client(actor=alice_id), DELETE, bob_user.pk)
        assert resp.status_code == 403
        ctx = UserPersonalContext.objects.get(user=bob_user)
        assert ctx.workplace_district == "Заводской"
        assert ctx.data_sources.get("workplace_district") == "explicit"

    def test_context_delete_of_a_foreign_subject_leaves_the_data_intact(self, alice, bob):
        """ВТОРОЙ маршрут стирания. Оба зовут ``erase_personal_context``;
        закрыть один из двух значило бы не закрыть ни одного."""
        _, alice_id = alice
        bob_user, _ = bob
        UserPersonalContext.objects.create(
            user=bob_user, workplace_district="Заводской",
            data_sources={"workplace_district": "explicit"},
        )
        resp = _call(_client(actor=alice_id), CTX_DELETE, bob_user.pk)
        assert resp.status_code == 403
        assert UserPersonalContext.objects.get(user=bob_user).workplace_district == "Заводской"

    def test_context_patch_of_a_foreign_subject_writes_nothing(self, alice, bob):
        """Не чтение — ЗАПИСЬ. Экспорт даёт копию, запись даёт власть."""
        _, alice_id = alice
        bob_user, _ = bob
        resp = _call(_client(actor=alice_id), CTX_PATCH, bob_user.pk)
        assert resp.status_code == 403
        ctx = UserPersonalContext.objects.filter(user=bob_user).first()
        assert ctx is None or ctx.workplace_district != "Центр"

    def test_deletion_request_for_a_foreign_subject_is_not_created(self, alice, bob):
        """Третий класс — запуск процедуры: заявка на удаление чужого человека."""
        from users.models import DeletionRequest

        _, alice_id = alice
        bob_user, _ = bob
        resp = _call(_client(actor=alice_id), DEL_REQ_POST, bob_user.pk)
        assert resp.status_code == 403
        assert not DeletionRequest.objects.filter(user=bob_user).exists()


class TestWrongPurposeDenied:
    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_provisioning_credential_is_refused(self, route, alice):
        alice_user, alice_id = alice
        resp = _call(_client(bearer=PROVISIONING_TOKEN, actor=alice_id), route, alice_user.pk)
        assert resp.status_code in (401, 403)


class TestUnnamedCallerIsRefused:
    """Без ``X-External-User-ID`` на этой поверхности не ходит никто.

    В #318 незнакомый зовущий СЧИТАЛСЯ, а не отказывался: бот тогда не
    называл субъект. С ai-bot-platform#1535 называет — и отказ стал
    возможен без потери боевого пути. Это и есть закрытие дыры: держатель
    токена, просто опустивший заголовок, больше не достаёт до субъекта.
    """

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_no_header_means_no_access(self, route, alice):
        alice_user, _ = alice
        resp = _call(_client(actor=None), route, alice_user.pk)
        assert resp.status_code == 403
        assert "X-External-User-ID" in resp.json()["error"]["message"]


class TestPreExistingPerimeter:
    """Четвёртый отрицательный из §7 — и что он доказывает, а что нет:
    проходил до этой правки, оставлен, чтобы его не добавили снова как
    доказательство границы ВНУТРИ периметра."""

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_invalid_token_denied(self, route, alice):
        alice_user, _ = alice
        assert _call(_client(bearer="nope"), route, alice_user.pk).status_code in (401, 403)

    def test_unset_runtime_token_fails_closed(self, settings, alice):
        alice_user, alice_id = alice
        settings.AYLA_INTERNAL_API_TOKEN = ""
        resp = _call(_client(bearer="", actor=alice_id), EXPORT, alice_user.pk)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Положительная половина: сторож «только отказ» неотличим от сломанной ручки
# ---------------------------------------------------------------------------


class TestOwnSubjectAllowed:
    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_caller_reaches_its_own_subject(self, route, alice):
        alice_user, alice_id = alice
        resp = _call(_client(actor=alice_id), route, alice_user.pk)
        # У списка заявок «заявки нет» — законный 404, но ПОСЛЕ проверки
        # субъекта: 403 здесь означал бы, что проверка не пустила своего.
        allowed = (200, 201, 404) if route == DEL_REQ_GET else (200, 201)
        assert resp.status_code in allowed, resp.content

    def test_binding_is_followed_not_bypassed(self, alice):
        """Заголовок называет PROXY-строку; авторизованный субъект — РЕАЛЬНЫЙ
        аккаунт, к которому она привязана. Разница между чтением графа
        личностей и сравнением двух строк."""
        alice_user, alice_id = alice
        proxy = User.objects.get(username=alice_id)
        assert proxy.pk != alice_user.pk
        assert _call(_client(actor=alice_id), EXPORT, alice_user.pk).status_code == 200
        assert _call(_client(actor=alice_id), EXPORT, proxy.pk).status_code == 403

    def test_unbound_proxy_reaches_only_itself(self):
        User.objects.create(username="bot:telegram:9009", role="client", is_proxy=True, is_guest=False)
        proxy = User.objects.get(username="bot:telegram:9009")
        stranger = User.objects.create_user(
            username="stranger", password="x", role="client", phone="+79990009999",
        )
        assert _call(_client(actor="bot:telegram:9009"), EXPORT, proxy.pk).status_code == 200
        assert _call(_client(actor="bot:telegram:9009"), EXPORT, stranger.pk).status_code == 403


class TestAuthorizationCreatesNothing:
    """Проверка не должна стать атакой: авторизующий резолв не создаёт строк."""

    def test_unknown_actor_is_refused_without_provisioning_a_row(self, alice):
        alice_user, _ = alice
        before = User.objects.count()
        resp = _call(_client(actor="bot:telegram:404404"), EXPORT, alice_user.pk)
        assert resp.status_code == 403
        assert User.objects.count() == before
        assert not User.objects.filter(username="bot:telegram:404404").exists()

    def test_malformed_actor_is_refused_without_provisioning_a_row(self, alice):
        alice_user, _ = alice
        before = User.objects.count()
        resp = _call(_client(actor="not a valid external id"), EXPORT, alice_user.pk)
        assert resp.status_code == 403
        assert User.objects.count() == before


# ---------------------------------------------------------------------------
# Сторож на СОСТАВ поверхности: маршрут девятый приедет через месяц
# ---------------------------------------------------------------------------

URLS = Path(__file__).resolve().parents[1] / "internal_users_urls.py"
#: DRF-1815 — маршруты с профилем мастера в URL живут в корневом urls:
#: ``/internal/specialists/<uuid:specialist_id>/…``. Тот же сторож состава.
ROOT_URLS = Path(__file__).resolve().parents[2] / "djangoProject" / "urls.py"

#: Маршруты с субъектом в URL, охраняемые ИНЫМ механизмом — с названной причиной.
#: Пустая причина не принимается: это список долгов, а не исключений.
GUARDED_OTHERWISE: dict[str, str] = {
    "internal-cards-setup": "IsBotServiceWithVerifiedClient + _check_user_scope в виде (C7.6)",
    "internal-cards-list": "IsBotServiceWithVerifiedClient + _check_user_scope в виде (C7.6)",
    "internal-cards-delete": "IsBotServiceWithVerifiedClient + _check_user_scope в виде (C7.6)",
}


def _subject_routes() -> list[tuple[str, str, str]]:
    """(name, kwarg, template) для каждого маршрута urls с ``<uuid:*user_id>``."""

    text = URLS.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(
        r'path\(\s*"([^"]*<uuid:(\w*user_id)>[^"]*)",.*?name="([^"]+)"', text, re.S
    ):
        template, kwarg, name = m.group(1), m.group(2), m.group(3)
        out.append((name, kwarg, template))
    return out


#: Маршруты ``/internal/specialists/<uuid:specialist_id>/…`` (корневой urls),
#: охраняемые иначе — с названной причиной. Профиль мастера в URL здесь —
#: не субъект, а ресурс салона: сторож — Bearer + заявленный ``tenant_id``,
#: чужой тенант отвечает 404 (DRF-1036: UUID не подтверждается).
GUARDED_OTHERWISE_SPECIALIST: dict[str, str] = {
    "internal-payout-preview": "IsInternalBearer + tenant_id как заявка, 404 на чужой (C3)",
    "internal-specialist-time-off": (
        "IsInternalBearer + tenant_id как заявка, 404 на чужой (DRF-1062)"
    ),
    "internal-specialist-schedule": (
        "IsInternalBearer + tenant_id как заявка, 404 на чужой (DRF-1126)"
    ),
}

#: Subject-маршруты мастера, чьи отрицательные тесты живут в своём наборе.
SPECIALIST_ROUTES_TESTED_ELSEWHERE: dict[str, str] = {
    "internal-specialist-working-hours": (
        "users/tests/test_internal_working_hours_1815.py::TestSubject"
    ),
}

_SPECIALIST_ROUTE_RE = re.compile(
    r"path\(\s*'(api/v1/internal/specialists/<uuid:(specialist_id)>/[^']*)',.*?name='([^']+)'",
    re.S,
)


def _specialist_subject_routes() -> list[tuple[str, str, str]]:
    """(name, kwarg, template) для корневых маршрутов с ``<uuid:specialist_id>``
    под ``api/v1/internal/specialists/``."""
    text = ROOT_URLS.read_text(encoding="utf-8")
    return [
        (m.group(3), m.group(2), m.group(1)) for m in _SPECIALIST_ROUTE_RE.finditer(text)
    ]


def _has_subject_guard(view_cls) -> bool:
    """Подкласс — тот же сторож (DRF-1815: ``subject_of`` меняет только,
    ЧТО сравнивать); проверка по идентичности класса пропустила бы его."""
    return any(
        isinstance(p, type) and issubclass(p, IsInternalBearerForSubject)
        for p in view_cls.permission_classes
    )


class TestGuardCoversItsSubject:
    def test_the_surface_is_enumerated(self):
        names = {n for n, _, _ in _subject_routes()}
        # Присутствие впереди отсутствия: обход нашёл известные маршруты.
        assert {"internal-personal-data-export", "internal-deletion-request-create"} <= names, names

    @pytest.mark.parametrize("route", _subject_routes(), ids=lambda r: r[0])
    def test_every_subject_route_carries_the_check_or_a_named_reason(self, route):
        name, kwarg, template = route
        if name in GUARDED_OTHERWISE:
            assert GUARDED_OTHERWISE[name].strip(), f"{name}: причина пустая"
            return
        sample = template.replace(f"<uuid:{kwarg}>", "11111111-1111-1111-1111-111111111111")
        sample = re.sub(r"<uuid:\w+>", "22222222-2222-2222-2222-222222222222", sample)
        match = resolve("/api/v1/internal/users/" + sample)
        view_cls = match.func.view_class
        assert _has_subject_guard(view_cls), (
            f"{name}: субъект в URL, а проверки субъекта нет"
        )
        assert getattr(view_cls, "subject_url_kwarg", None) == kwarg, (
            f"{name}: subject_url_kwarg={getattr(view_cls, 'subject_url_kwarg', None)!r}, "
            f"а в URL {kwarg!r} — проверка сравнивала бы не то"
        )

    def test_specialist_routes_are_enumerated(self):
        names = {n for n, _, _ in _specialist_subject_routes()}
        assert {"internal-specialist-schedule", "internal-specialist-working-hours"} <= names, names

    @pytest.mark.parametrize("route", _specialist_subject_routes(), ids=lambda r: r[0])
    def test_every_specialist_route_carries_the_check_or_a_named_reason(self, route):
        """DRF-1815 — профиль мастера в URL: либо субъектный сторож
        (подкласс ``IsInternalBearerForSubject`` c ``subject_url_kwarg``),
        либо названная причина, почему иначе."""
        name, kwarg, template = route
        if name in GUARDED_OTHERWISE_SPECIALIST:
            assert GUARDED_OTHERWISE_SPECIALIST[name].strip(), f"{name}: причина пустая"
            return
        sample = template.replace(f"<uuid:{kwarg}>", "11111111-1111-1111-1111-111111111111")
        view_cls = resolve("/" + sample).func.view_class
        assert _has_subject_guard(view_cls), f"{name}: профиль в URL, а проверки субъекта нет"
        assert getattr(view_cls, "subject_url_kwarg", None) == kwarg, (
            f"{name}: subject_url_kwarg={getattr(view_cls, 'subject_url_kwarg', None)!r}"
        )
        assert name in SPECIALIST_ROUTES_TESTED_ELSEWHERE, (
            f"{name}: субъектный маршрут без отрицательных тестов"
        )

    def test_the_route_list_here_is_the_whole_guarded_surface(self):
        """Новый маршрут с субъектом обязан попасть либо в ALL_ROUTES, либо в
        GUARDED_OTHERWISE — иначе на него нет ни одного отрицательного теста."""

        guarded = set()
        for name, kwarg, template in _subject_routes():
            if name in GUARDED_OTHERWISE:
                continue
            guarded.add("/api/v1/internal/users/" + template.replace(f"<uuid:{kwarg}>", "{subject}"))
        tested = {t for _, t in ALL_ROUTES}
        # detail-маршрут заявки требует существующей заявки; покрыт классом, не вызовом.
        tested.add("/api/v1/internal/users/{subject}/deletion-requests/<uuid:request_id>/")
        # DRF-1666 — запись Recommendation: маршруты требуют существующего набора/записи
        # субъекта; отрицательные тесты (чужой субъект → 404 после проверки, несовпадение /
        # без заголовка / чужой bearer → 403) — recommendation/tests/test_record_api.py.
        tested.add("/api/v1/internal/users/{subject}/recommendations/<uuid:set_id>/")
        tested.add("/api/v1/internal/users/{subject}/recommendations/<uuid:recommendation_id>/events/")
        assert guarded <= tested, f"маршруты без отрицательного теста: {sorted(guarded - tested)}"
