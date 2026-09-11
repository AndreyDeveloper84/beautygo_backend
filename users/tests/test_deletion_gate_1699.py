"""D2 — стоп персонализации по живой заявке на удаление (§7 свода, DRF-1699).

§7: «персонализация и новая обработка данных прекращаются сразу» — с
момента приёма заявки. Три класса читателей — память (персональный
контекст), рекомендации (две ручки), проактив (спросить / отметить /
пропустить) — при живой заявке отвечают отказом С ИМЕНЕМ: 423
``DELETION_IN_PROGRESS`` с ``request_id`` в ``details``, ask-eligibility —
``should_ask: false, blocked_by: deletion_requested``. Не пустым ответом:
пустота читалась бы как «новый человек» и включила бы сбор заново.

У каждого отказа — парная положительная стража на тех же данных: без
заявки (или с завершённой) та же ручка отвечает как раньше.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from recommendation.tests.test_resolve_endpoint import _body as resolve_body
from users.models import DeletionRequest, User, UserPersonalContext

pytestmark = pytest.mark.django_db

TOKEN = "test-bearer-d2-1699"  # noqa: S105
EXTERNAL_USER_ID = "bot:d2-1699"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79994211699", is_proxy=True,
    )


def _open_request(user, status=DeletionRequest.Status.REQUESTED):
    return DeletionRequest.objects.create(
        user=user, initiator="bot", status=status,
        deadline_at=timezone.now() + timedelta(days=30),
    )


def _completed_request(user):
    return DeletionRequest.objects.create(
        user=user, initiator="bot", status=DeletionRequest.Status.COMPLETED,
        deadline_at=timezone.now(), completed_at=timezone.now(),
    )


def _assert_refusal(r, req):
    assert r.status_code == 423, r.content
    err = r.json()["error"]
    assert err["code"] == "DELETION_IN_PROGRESS"
    assert err["details"]["reason"] == "deletion_requested"
    assert err["details"]["request_id"] == str(req.pk)


# ---------------------------------------------------------------------------
# Память — персональный контекст
# ---------------------------------------------------------------------------


class TestPersonalContextIsLocked:
    URL = "/api/v1/internal/users/{}/personal-context/"

    def test_get_is_423_with_the_request_id(self, api, user):
        UserPersonalContext.objects.create(user=user, diet_type="vegan")
        req = _open_request(user)

        r = api.get(self.URL.format(user.pk))

        _assert_refusal(r, req)
        # Ни одного поля контекста в теле отказа: заперто — значит заперто.
        assert "vegan" not in r.content.decode()

    def test_patch_is_423_and_writes_nothing(self, api, user):
        ctx = UserPersonalContext.objects.create(user=user, diet_type="vegan")
        req = _open_request(user)

        r = api.patch(
            self.URL.format(user.pk),
            {"updates": [{"field": "diet_type", "value": "keto"}]},
            format="json",
        )

        _assert_refusal(r, req)
        ctx.refresh_from_db()
        assert ctx.diet_type == "vegan"

    def test_without_a_request_the_context_is_served(self, api, user):
        """Положительная стража: гейт закрывает НЕ всё."""
        UserPersonalContext.objects.create(user=user, diet_type="vegan")
        assert api.get(self.URL.format(user.pk)).status_code == 200

    def test_a_completed_request_does_not_lock(self, api, user):
        """Закрывает только открытая: после COMPLETED человек — обычный."""
        _completed_request(user)
        assert api.get(self.URL.format(user.pk)).status_code == 200

    def test_a_failed_request_still_locks(self, api, user):
        """FAILED — открытая (D1): сбой исполнителя не снимает стоп."""
        req = _open_request(user, status=DeletionRequest.Status.FAILED)
        _assert_refusal(api.get(self.URL.format(user.pk)), req)


# ---------------------------------------------------------------------------
# Проактив — спросить / отметить / пропустить
# ---------------------------------------------------------------------------


class TestProactiveIsLocked:
    BASE = "/api/v1/internal/users/{}/personal-context/"

    def test_ask_eligibility_says_no_with_the_name(self, api, user):
        req = _open_request(user)

        r = api.get(self.BASE.format(user.pk) + "ask-eligibility/")

        assert r.status_code == 200, r.content
        data = r.json()["data"]
        assert data["should_ask"] is False
        assert data["blocked_by"] == "deletion_requested"
        assert data["request_id"] == str(req.pk)

    def test_ask_eligibility_without_a_request_is_the_engine_answer(self, api, user):
        r = api.get(self.BASE.format(user.pk) + "ask-eligibility/")
        assert r.status_code == 200, r.content
        # Что бы ни ответил движок, причина — его, не наша.
        assert r.json()["data"].get("blocked_by") != "deletion_requested"

    def test_mark_asked_and_skip_are_423(self, api, user):
        req = _open_request(user)
        body = {"field": "diet_type"}

        _assert_refusal(api.post(self.BASE.format(user.pk) + "mark-asked/", body, format="json"), req)
        _assert_refusal(api.post(self.BASE.format(user.pk) + "skip/", body, format="json"), req)

    def test_mark_asked_and_skip_work_without_a_request(self, api, user):
        body = {"field": "diet_type"}
        assert api.post(self.BASE.format(user.pk) + "mark-asked/", body, format="json").status_code == 200
        assert api.post(self.BASE.format(user.pk) + "skip/", body, format="json").status_code == 200


# ---------------------------------------------------------------------------
# Рекомендации — обе ручки
# ---------------------------------------------------------------------------


class TestRecommendationsAreLocked:
    CATALOG_URL = "/api/v1/internal/me/catalog/recommendations/"
    RESOLVE_URL = "/api/v1/internal/recommendation/resolve/"

    def test_catalog_recommendations_are_423(self, api, user):
        req = _open_request(user)
        _assert_refusal(api.post(self.CATALOG_URL, {"goal": "маникюр"}, format="json"), req)

    def test_catalog_recommendations_work_without_a_request(self, api, user):
        assert api.post(self.CATALOG_URL, {"goal": "маникюр"}, format="json").status_code == 200

    def test_resolve_is_423_before_the_body_is_even_read(self, api, user, settings):
        """Отказ по воле человека — до разбора тела: кривое тело даёт 423, не 400."""
        settings.RECOMMENDATION_CANDIDATE_SOURCE = (
            "recommendation.tests.test_resolve_endpoint.fixture_source_factory"
        )
        req = _open_request(user)
        _assert_refusal(api.post(self.RESOLVE_URL, {"garbage": True}, format="json"), req)

    def test_resolve_works_without_a_request(self, api, user, settings):
        settings.RECOMMENDATION_CANDIDATE_SOURCE = (
            "recommendation.tests.test_resolve_endpoint.fixture_source_factory"
        )
        r = api.post(self.RESOLVE_URL, resolve_body(), format="json")
        assert r.status_code == 200, r.content
