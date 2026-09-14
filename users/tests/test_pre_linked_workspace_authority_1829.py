"""DRF-1829 (M28): pre-LINKED принципал — владелец provisioned DRAFT workspace.

Решение владельца 12.09 (G1 → б, PROMPT §1, §17): до LINKED мастер настраивает
своё рабочее пространство (часы, услуги, места, профиль), но это **workspace
setup authority, не identity authority**. Setup-write привязан к конкретному
provisioned workspace; «внутренний токен + произвольный specialist_id → пиши
что угодно» — запрещено.

Предмет — ``IsInternalBearerForSpecialistSubject`` на первой setup-ручке
``PUT/GET /internal/specialists/{id}/working-hours/`` (#423). §17 перечисляет,
на что ответ обязан быть NO; каждый такой тест стоит рядом с положительной
половиной на тех же данных, иначе «не пустили» зеленело бы и на сломанной
фикстуре.

Красный до правки — четыре теста, у всех отказывает положительная половина
(свой DRAFT-workspace → 403): ``test_own_draft_workspace_without_a_proxy_row``,
``test_own_draft_workspace_with_an_unlinked_proxy``,
``test_get_reads_back_what_the_owner_wrote`` и
``test_another_masters_workspace`` (его отказ на чужой workspace зелёный в обе
стороны, а «свой — можно» до правки красный). Остальные NO-тесты и тест после
LINKED — зелёные в обе стороны: они стерегут, что правка не открыла лишнего.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from tenants.solo_provisioning import provision_solo_workspace
from users.models import SpecialistProfile, User
from users.services import bind_external_identity_by_operator
from users.tests.test_internal_working_hours_1815 import _hours, _url, _week

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1829"  # noqa: S105
PROVISIONING_TOKEN = "test-tenant-provisioning-token-1829"  # noqa: S105

OLGA = "bot:max:1829001"
IRINA = "bot:max:1829002"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING_TOKEN


def _provision(external_user_id: str, slug: str) -> SpecialistProfile:
    workspace = provision_solo_workspace(
        tenant_id=uuid.uuid4(),
        slug=slug,
        name=f"Студия {slug}",
        city="Пенза",
        external_user_id=external_user_id,
        display_name="Мастер",
    )
    return workspace.profile


@pytest.fixture
def olga() -> SpecialistProfile:
    return _provision(OLGA, "solo-max-1829olga")


@pytest.fixture
def irina() -> SpecialistProfile:
    return _provision(IRINA, "solo-max-1829irin")


def _client(*, bearer: str | None = RUNTIME_TOKEN, actor: str | None = None) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _unlinked_proxy(external_user_id: str) -> User:
    return User.objects.create(
        username=external_user_id, role="client", is_proxy=True, is_guest=False,
    )


class TestTheOwnerSetsUpTheirDraftWorkspace:
    def test_own_draft_workspace_without_a_proxy_row(self, olga):
        """Provisioning прокси не заводит — бот мог ещё ни разу не представиться."""
        assert not User.objects.filter(username=OLGA).exists()

        resp = _client(actor=OLGA).put(_url(olga.pk), _week(), format="json")

        assert resp.status_code == 200, resp.content
        assert _hours(olga) != {}
        # Проверка права не завела личность задним числом.
        assert not User.objects.filter(username=OLGA).exists()

    def test_own_draft_workspace_with_an_unlinked_proxy(self, olga):
        _unlinked_proxy(OLGA)

        resp = _client(actor=OLGA).put(_url(olga.pk), _week(), format="json")

        assert resp.status_code == 200, resp.content
        assert _hours(olga) != {}

    def test_get_reads_back_what_the_owner_wrote(self, olga):
        assert _client(actor=OLGA).put(_url(olga.pk), _week(), format="json").status_code == 200
        resp = _client(actor=OLGA).get(_url(olga.pk))
        assert resp.status_code == 200, resp.content


class TestSection17AnswersNo:
    """§17 PROMPT: до LINKED — нельзя чужое. Ответ везде NO."""

    def test_another_masters_workspace(self, olga, irina):
        """Чужой specialist_id / чужой solo tenant: заголовок Ольги, URL Ирины."""
        resp = _client(actor=OLGA).put(_url(irina.pk), _week(), format="json")
        assert resp.status_code == 403, resp.content
        assert _hours(irina) == {}
        # Положительная половина на тех же данных: свой — можно.
        assert _client(actor=OLGA).put(_url(olga.pk), _week(), format="json").status_code == 200

    def test_a_salon_masters_profile_is_not_a_workspace_to_claim(self, olga):
        """Подобранный UUID обычного мастера (claim не его) — нет."""
        salon_user = User.objects.create_user(
            username="salon_1829", password="x", role="specialist", phone="+79995182901",  # pragma: allowlist secret
        )
        salon_profile = SpecialistProfile.objects.get(user=salon_user)

        resp = _client(actor=OLGA).put(_url(salon_profile.pk), _week(), format="json")

        assert resp.status_code == 403, resp.content
        assert _hours(salon_profile) == {}

    def test_a_published_workspace_is_not_open_to_the_claim(self, olga):
        """Claim — про настройку DRAFT; после публикации — только связанный субъект."""
        olga.status = SpecialistProfile.ProfileStatus.ACTIVE
        olga.save(update_fields=["status"])

        resp = _client(actor=OLGA).put(_url(olga.pk), _week(), format="json")

        assert resp.status_code == 403, resp.content
        assert _hours(olga) == {}

    def test_an_identity_linked_elsewhere_does_not_fall_back_to_the_claim(self, olga):
        """Заголовок уже связан с другим мастером — связь личности старше claim."""
        other = User.objects.create_user(
            username="other_1829", password="x", role="specialist", phone="+79995182902",  # pragma: allowlist secret
        )
        User.objects.create(
            username=OLGA, role="client", is_proxy=True, is_guest=False, linked_user=other,
        )

        resp = _client(actor=OLGA).put(_url(olga.pk), _week(), format="json")

        assert resp.status_code == 403, resp.content
        assert _hours(olga) == {}

    def test_the_provisioning_token_is_not_a_generic_write_credential(self, olga):
        resp = _client(bearer=PROVISIONING_TOKEN, actor=OLGA).put(
            _url(olga.pk), _week(), format="json",
        )
        assert resp.status_code == 403, resp.content
        assert _hours(olga) == {}

    def test_the_runtime_token_without_a_named_actor_is_not_enough(self, olga):
        resp = _client(actor=None).put(_url(olga.pk), _week(), format="json")
        assert resp.status_code == 403, resp.content
        assert _hours(olga) == {}


class TestAfterLinkedTheSameRequestIsTheSubjects:
    def test_linked_owner_keeps_access_through_the_subject_path(self, olga):
        """G1: при LINKED профиль тот же; доступ переезжает на субъект, а после
        публикации claim ничего не открывает — субъект открывает."""
        _unlinked_proxy(OLGA)
        operator = User.objects.create_superuser(
            username="op-1829", password="pw", email="op-1829@x.y", role="admin",  # pragma: allowlist secret
        )
        bind_external_identity_by_operator(OLGA, olga.user_id, actor=operator)

        assert _client(actor=OLGA).put(_url(olga.pk), _week(), format="json").status_code == 200

        olga.status = SpecialistProfile.ProfileStatus.ACTIVE
        olga.save(update_fields=["status"])
        resp = _client(actor=OLGA).put(_url(olga.pk), _week(), format="json")
        assert resp.status_code == 200, resp.content
