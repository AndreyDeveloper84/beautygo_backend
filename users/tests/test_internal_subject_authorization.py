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
# DRF-1855: отзыв клиента из бота — запись от имени субъекта.
REVIEW_POST = ("post", "/api/v1/internal/users/{subject}/reviews/")
# DRF-1888: вход записи Recommendation для производителя NBA — запись от имени субъекта.
REC_SET_POST = ("post", "/api/v1/internal/users/{subject}/recommendation-sets/")
# DRF-1984 (C5.3): readback стирания — чтение состояния без значений.
ERASURE_STATUS = ("get", "/api/v1/internal/users/{subject}/personal-data/erasure-status/")

ALL_ROUTES = [
    EXPORT, DELETE, CTX_GET, CTX_PATCH, CTX_DELETE, CTX_ELIG, CTX_ASKED, CTX_SKIP,
    DEL_REQ_GET, DEL_REQ_POST, PROFILE, REVIEW_POST, REC_SET_POST, ERASURE_STATUS,
]

_BODIES = {
    CTX_PATCH: {"updates": [{"field": "workplace_district", "value": "Центр"}]},
    CTX_ASKED: {"field": "preferred_time_slots"},
    CTX_SKIP: {"field": "preferred_time_slots"},
    DEL_REQ_POST: {"initiator": "bot"},
    REVIEW_POST: {"appointment_id": "00000000-0000-0000-0000-000000000000", "rating": 5},
    REC_SET_POST: {"intent_id": "intent-authz"},
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
        if route == REC_SET_POST:
            # DRF-1888: X-Idempotency-Key обязателен, общий _call его не шлёт —
            # 400 IDEMPOTENCY_KEY_REQUIRED отвечает ручка ПОСЛЕ проверки
            # субъекта; 403 означал бы, что проверка не пустила своего.
            assert resp.status_code == 400, resp.content
            assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED", resp.content
            return
        # У списка заявок «заявки нет» — законный 404, но ПОСЛЕ проверки
        # субъекта: 403 здесь означал бы, что проверка не пустила своего.
        # У отзыва (DRF-1855) тело называет несуществующую бронь — 404 тоже
        # после проверки субъекта.
        allowed = (200, 201, 404) if route in (DEL_REQ_GET, REVIEW_POST) else (200, 201)
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
# DRF-1947 — неактивный, удалённый и tombstone-субъект
# ---------------------------------------------------------------------------

#: Ручки, где неактивный/удалённый субъект законен: стирание уже удалённого
#: через бота должно работать (докстринг ``users.services._follow_binding``).
ERASURE_ROUTES = [DEL_REQ_GET, DEL_REQ_POST, DELETE, ERASURE_STATUS]

#: Классы view, которым разрешён неактивный субъект, — ровно эти (решение
#: главного окна 15.09). Сторож ниже сверяет с живым набором URL.
ALLOW_INACTIVE_SUBJECT_VIEWS = {
    "InternalDeletionRequestCreateView",
    "InternalDeletionRequestDetailView",
    "InternalPersonalDataDeleteView",
    # DRF-1984 — подтверждение стирания уже удалённого читается у удалённого.
    "InternalPersonalDataErasureStatusView",
}


def _deactivate(user: User, *, deleted: bool) -> User:
    from django.utils import timezone

    user.is_active = False
    fields = ["is_active"]
    if deleted:
        user.deleted_at = timezone.now()
        fields.append("deleted_at")
    user.save(update_fields=fields)
    return user


def _as_after_d3(user: User) -> None:
    """Состояние после исполнителя D3: аккаунт и прокси — ``deleted:<pk>``."""
    from django.utils import timezone

    for row in [user, *User.objects.filter(linked_user=user)]:
        row.username = f"deleted:{row.pk}"
        row.is_active = False
        row.deleted_at = row.deleted_at or timezone.now()
        row.save(update_fields=["username", "is_active", "deleted_at"])


def _expect_refused(resp, route, state):
    assert resp.status_code == 403, f"{state}: {route} пустил субъекта: {resp.status_code}"


def _expect_allowed(resp, route, state):
    assert resp.status_code in (200, 201, 404), (
        f"{state}: {route} — ручка стирания должна пускать: {resp.status_code} {resp.content!r}"
    )


class TestInactiveOrDeletedSubject:
    """Состояние субъекта, а не написание имени, решает доступ (DRF-1947)."""

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_inactive_subject(self, route, alice):
        user, actor = alice
        _deactivate(user, deleted=False)
        resp = _call(_client(actor=actor), route, user.pk)
        if route in ERASURE_ROUTES:
            _expect_allowed(resp, route, "inactive")
        else:
            _expect_refused(resp, route, "inactive")

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_deleted_before_d3(self, route, alice):
        """Удаление из приложения: is_active=False + deleted_at, привязка прокси жива."""
        user, actor = alice
        _deactivate(user, deleted=True)
        resp = _call(_client(actor=actor), route, user.pk)
        if route in ERASURE_ROUTES:
            _expect_allowed(resp, route, "deleted-before-d3")
        else:
            _expect_refused(resp, route, "deleted-before-d3")

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_deleted_after_d3(self, route, alice):
        """После D3 заголовок бота не резолвится — отказ везде (страж регрессии)."""
        user, actor = alice
        _as_after_d3(user)
        _expect_refused(_call(_client(actor=actor), route, user.pk), route, "deleted-after-d3")

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_reserved_deleted_header(self, route, alice):
        """``deleted:<pk>`` — зарезервированный источник, не внешняя личность."""
        user, _ = alice
        _as_after_d3(user)
        resp = _call(_client(actor=f"deleted:{user.pk}"), route, user.pk)
        _expect_refused(resp, route, "deleted-header")

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=_ids)
    def test_tombstone_subject(self, route):
        """Прокси, привязанный к tombstone: отказ везде, и на стирании тоже —
        стирание tombstone обезличило бы клиента всех перевешанных на него записей."""
        from users.deletion_executor import tombstone_user

        tomb = tombstone_user()
        User.objects.create(
            username="bot:telegram:7007", role="client", is_proxy=True, is_guest=False,
            linked_user=tomb,
        )
        resp = _call(_client(actor="bot:telegram:7007"), route, tomb.pk)
        _expect_refused(resp, route, "tombstone")

    def test_erasure_of_an_already_deleted_subject_still_works(self, alice):
        """Ровно то, что держат исключения: стирание удалённого через бота."""
        from users.models import DeletionRequest

        user, actor = alice
        UserPersonalContext.objects.create(
            user=user, workplace_district="Заводской",
            data_sources={"workplace_district": "explicit"},
        )
        _deactivate(user, deleted=True)
        client = _client(actor=actor)

        resp = _call(client, DELETE, user.pk)
        assert resp.status_code == 200, resp.content
        ctx = UserPersonalContext.objects.filter(user=user).first()
        assert ctx is None or ctx.workplace_district != "Заводской"
        resp = _call(client, DEL_REQ_POST, user.pk)
        assert resp.status_code in (200, 201), resp.content
        assert DeletionRequest.objects.filter(user=user).exists()


class TestReservedAndInactiveExternalIds:
    def test_reserved_source_and_tombstone_are_not_external_ids(self, alice):
        from users.deletion_executor import TOMBSTONE_USERNAME
        from users.services import is_valid_external_user_id

        user, actor = alice
        # Присутствие впереди отсутствия: обычная внешняя личность валидна.
        assert is_valid_external_user_id(actor) is True
        assert is_valid_external_user_id(f"deleted:{user.pk}") is False
        assert is_valid_external_user_id(TOMBSTONE_USERNAME) is False

    def test_acting_resolver_refuses_the_reserved_source_and_creates_nothing(self):
        import uuid

        from users.services import InvalidExternalUserIDError, resolve_external_user

        header = f"deleted:{uuid.uuid4()}"
        before = User.objects.count()
        with pytest.raises(InvalidExternalUserIDError):
            resolve_external_user(header)
        assert User.objects.count() == before

    def test_acting_resolver_refuses_an_inactive_non_proxy_row(self):
        from users.services import InvalidExternalUserIDError, resolve_external_user

        User.objects.create_user(
            username="legacy:4242", password="x", role="client", phone="+79990004242",
        )
        row = User.objects.get(username="legacy:4242")
        # Присутствие: активная строка резолвится в себя.
        assert resolve_external_user("legacy:4242").pk == row.pk
        _deactivate(row, deleted=True)
        with pytest.raises(InvalidExternalUserIDError):
            resolve_external_user("legacy:4242")

    def test_acting_route_refuses_the_reserved_header(self, alice):
        user, _ = alice
        _as_after_d3(user)
        resp = _client(actor=f"deleted:{user.pk}").get("/api/v1/internal/me/identity/")
        assert resp.status_code == 403, resp.content


class TestAllowInactiveSubjectIsNamed:
    def test_only_the_named_erasure_views_allow_an_inactive_subject(self):
        from django.urls import get_resolver

        found: dict[str, str] = {}

        def walk(patterns):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    walk(p.url_patterns)
                    continue
                cls = getattr(getattr(p, "callback", None), "view_class", None)
                reason = getattr(cls, "allow_inactive_subject", "") if cls else ""
                if reason:
                    found[cls.__name__] = reason

        walk(get_resolver().url_patterns)
        assert set(found) == ALLOW_INACTIVE_SUBJECT_VIEWS, found
        assert all(len(r.strip()) >= 20 for r in found.values()), found


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
    # DRF-1813 (M21): чужой workspace → 403, чужой элемент портфолио → 404;
    # отказы — рядом с положительной половиной.
    "internal-specialist-profile": (
        "users/tests/test_specialist_profile_m21_1813.py::TestSubject"
    ),
    "internal-specialist-avatar": (
        "users/tests/test_specialist_profile_m21_1813.py::TestSubject"
    ),
    "internal-specialist-portfolio": (
        "users/tests/test_specialist_profile_m21_1813.py::TestSubject"
    ),
    "internal-specialist-portfolio-item": (
        "users/tests/test_specialist_profile_m21_1813.py::TestPortfolio"
    ),
    # DRF-1801 (M9) — заявки мастера о разрыве канона.
    "internal-specialist-canon-gap-requests": "services/tests/test_canon_gap_request_m9.py::TestSubject",
    "internal-specialist-canon-gap-similar": "services/tests/test_canon_gap_request_m9.py::TestSubject",
    "internal-specialist-canon-gap-request": "services/tests/test_canon_gap_request_m9.py::TestSubject",
    # DRF-1804 (M12a) — подсказки адреса: чужой workspace → 403, салон → 409,
    # без города → 409 без запроса к провайдеру; каждый отказ — с положительной половиной.
    "internal-specialist-address-suggest": "users/tests/test_address_suggest_1804.py::TestSubject",
    # DRF-1800 (M8a): чужой workspace → 403, мастер салона → 409, без
    # положительной половины ни одного отказа.
    "internal-specialist-service-selection": (
        "services/tests/test_offer_selection_m8a_1800.py::TestRefusals"
    ),
    # DRF-1800 (M8b): чужой workspace по URL → 403, услуга не из своего
    # workspace → 404; каждый отказ — с положительной половиной.
    "internal-specialist-service-offer": (
        "services/tests/test_offer_price_m8b_1800.py::TestOfferRefusals"
    ),
    "internal-specialist-selected-service": (
        "services/tests/test_offer_price_m8b_1800.py::TestRemoval"
    ),
    "internal-specialist-availability": (
        "users/tests/test_internal_availability_1845.py::TestSubject"
    ),
    "internal-specialist-reviews": (
        "users/tests/test_internal_specialist_reviews_1857.py::TestSubject"
    ),
    # DRF-1796 (M4): чужой workspace → 403, салон → 409, неготовность → 409;
    # отказы — рядом с положительной половиной.
    "internal-specialist-publication-readiness": (
        "users/tests/test_publication_m4_1796.py::TestPublish"
    ),
    "internal-specialist-publication-status": (
        "users/tests/test_publication_m4_1796.py::TestPublish"
    ),
    "internal-specialist-publication": (
        "users/tests/test_publication_m4_1796.py::TestPublish"
    ),
    # DRF-1803 (M11) — место соло-мастера: чужой workspace → 403, салон → 409,
    # чужое место или зона → 404; каждый отказ — рядом с положительной половиной.
    "internal-specialist-service-locations": (
        "tenants/tests/test_master_places_1803.py::TestSubject"
    ),
    "internal-specialist-service-location": (
        "tenants/tests/test_master_places_1803.py::TestSubject"
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
        # Второй UUID в пути (DRF-1801: ``…/canon-gap-requests/<uuid:request_id>/``)
        # — тот же приём, что у маршрутов ``users`` выше: иначе маршрут не
        # разрешается, и сторож падает раньше своей проверки.
        sample = re.sub(r"<uuid:\w+>", "22222222-2222-2222-2222-222222222222", sample)
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
