"""DRF-2450 (вариант А): дверь связи личности сверяет соло-профиль с claim.

У соло-мастера приглашения нет — бот ведёт связь сразу после провижининга
(§77 п.38: «мне не надо участия человека в регистрации мастеров»).
Доказательство владения — сам провижининг, поэтому для соло-профиля
``external_user_id`` в теле обязан равняться
``SpecialistProfile.provisioned_external_user_id``; иначе ``claim_mismatch``.

Узлы стоят на парах, которые обязаны различаться:

* тот же соло-профиль: своя личность — связь, чужая — ``claim_mismatch``;
* та же чужая личность: у мастера салона — связь (сверка его не касается),
  у соло — отказ;
* claim стёрт у соло-профиля — отказ (владение не доказано, fail-closed);
* подмена: сверка выключена — чужая личность проходит, узел краснеет.

Claim — проверка согласованности, не источник личности: он может только
отказать. Сторож «резолверы claim не читают» остаётся в силе
(``tenants/tests/test_solo_workspace_provisioning_1828.py``).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tenants.models import Tenant
from tenants.solo_provisioning import provision_solo_workspace
from users.models import SpecialistIdentityLinkRequest, SpecialistProfile, User
from users.services import bind_external_identity_by_operator, resolve_external_user_readonly
from users.specialist_identity_linking import (
    REASON_CLAIM_MISMATCH,
    SpecialistIdentityLinkRefused,
    link_specialist_identity,
)

pytestmark = pytest.mark.django_db

DOOR = "test-specialist-identity-link-2450"  # noqa: S105
GENERAL = "test-general-bot-token-2450"  # noqa: S105

OWN = "bot:max:2450001"  # claim провижининга
FOREIGN = "bot:max:2450999"  # другой человек
ACTOR = "solo_provisioning"


@pytest.fixture(autouse=True)
def tokens(settings):
    settings.AYLA_SPECIALIST_IDENTITY_LINK_TOKEN = DOOR
    settings.AYLA_INTERNAL_API_TOKEN = GENERAL
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = "test-identity-provisioning-2450"  # noqa: S105
    settings.AYLA_TENANT_PROVISIONING_TOKEN = "test-tenant-provisioning-2450"  # noqa: S105
    settings.AYLA_SALON_ADMIN_LINK_TOKEN = "test-salon-admin-link-2450"  # noqa: S105


@pytest.fixture(autouse=True)
def _clean_throttle_history():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def solo() -> SpecialistProfile:
    """Соло-профиль тем же путём, что на пилоте: сервис провижининга."""
    workspace = provision_solo_workspace(
        tenant_id=uuid.uuid4(),
        slug="solo-2450",
        name="Соло 2450",
        city="Москва",
        external_user_id=OWN,
        display_name="Мастер 2450",
    )
    assert workspace.tenant.kind == Tenant.Kind.SOLO
    assert workspace.profile.provisioned_external_user_id == OWN
    return workspace.profile


@pytest.fixture
def salon_master() -> SpecialistProfile:
    salon = Tenant.objects.create(slug="salon-2450", name="Салон 2450")
    user = User.objects.create_user(username="salon-2450-m1", password="x", role="specialist")
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    assert profile.provisioned_external_user_id is None
    return profile


def _proxy(external: str) -> User:
    """Личность, которую бот уже предъявлял каталогу, ещё не связанная."""
    return User.objects.create(username=external, role="client", is_proxy=True, is_guest=False)


def _door_post(profile: SpecialistProfile, external: str, key: str = "idem-2450-0001"):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {DOOR}")
    return c.post(
        f"/api/v1/internal/specialists/{profile.pk}/identity/",
        {"external_user_id": external, "actor": ACTOR, "idempotency_key": key},
        format="json",
    )


def _nothing_written(proxy: User) -> None:
    proxy.refresh_from_db()
    assert proxy.linked_user_id is None
    assert SpecialistIdentityLinkRequest.objects.count() == 0


@contextmanager
def _capturing(name: str, caplog):
    """``users`` стоит ``propagate=False`` — перехват на именованный логгер."""
    target = logging.getLogger(name)
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=name):
            yield
    finally:
        target.removeHandler(caplog.handler)


# ─── пара: тот же соло-профиль, своя личность против чужой ─────────────────


class TestSoloProfileLinksOnlyItsOwnClaim:
    def test_the_claimed_identity_is_linked_and_resolves_to_the_workspace(self, solo) -> None:
        proxy = _proxy(OWN)

        response = _door_post(solo, OWN)

        assert response.status_code == 201, response.content
        assert response.json()["data"]["specialist_id"] == str(solo.pk)
        proxy.refresh_from_db()
        assert proxy.linked_user_id == solo.user_id
        # связь, а не форточка pre-LINKED: резолвер ведёт на рабочую учётку
        assert resolve_external_user_readonly(OWN).pk == solo.user_id

    def test_a_foreign_identity_is_claim_mismatch_and_nothing_is_written(self, solo) -> None:
        _proxy(OWN)
        foreign = _proxy(FOREIGN)

        response = _door_post(solo, FOREIGN)

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == REASON_CLAIM_MISMATCH
        _nothing_written(foreign)

    def test_the_mismatch_is_named_before_the_identity_is_looked_up(self, solo) -> None:
        """Прокси чужой личности нет вовсе — ответ всё равно ``claim_mismatch``,
        а не ``identity_unknown``: сверка идёт до любой записи и любого поиска."""
        response = _door_post(solo, FOREIGN)

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == REASON_CLAIM_MISMATCH
        assert not User.objects.filter(username=FOREIGN).exists()


# ─── пара: та же чужая личность, мастер салона против соло ─────────────────


class TestTheCheckIsSoloOnly:
    def test_a_salon_master_is_not_checked_against_any_claim(self, solo, salon_master) -> None:
        """Доказательство у мастера салона — приглашение; claim у него нет,
        и сверка его не касается. Та же ``FOREIGN``, что у соло даёт отказ."""
        proxy = _proxy(FOREIGN)

        response = _door_post(salon_master, FOREIGN)

        assert response.status_code == 201, response.content
        proxy.refresh_from_db()
        assert proxy.linked_user_id == salon_master.user_id

    def test_a_solo_profile_without_a_claim_is_refused(self, solo) -> None:
        """Claim стёрт (ручная правка, откат) — тенант всё ещё ``kind=solo``.
        Владение не доказано ничем: fail-closed, даже для «своей» личности."""
        SpecialistProfile.objects.filter(pk=solo.pk).update(provisioned_external_user_id=None)
        proxy = _proxy(OWN)

        response = _door_post(solo, OWN)

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == REASON_CLAIM_MISMATCH
        _nothing_written(proxy)


# ─── повтор по ключу не обходит сверку ─────────────────────────────────────


class TestReplayIsCheckedToo:
    def test_a_row_written_before_the_rule_does_not_replay_a_mismatch(self, solo) -> None:
        """Строка запроса с чужой личностью (записана до этого листа) — повтор
        с тем же ключом тоже получает ``claim_mismatch``, а не ``replayed``."""
        proxy = _proxy(FOREIGN)
        proxy.linked_user_id = solo.user_id
        proxy.save(update_fields=["linked_user"])
        SpecialistIdentityLinkRequest.objects.create(
            idempotency_key="idem-2450-0001",
            profile=solo,
            user=solo.user,
            external_user_id=FOREIGN,
            actor="bot:onboarding_accept",
            result="created",
        )

        response = _door_post(solo, FOREIGN)

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == REASON_CLAIM_MISMATCH

    def test_the_own_identity_replays(self, solo) -> None:
        _proxy(OWN)
        first = _door_post(solo, OWN)
        second = _door_post(solo, OWN)

        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()["data"]["status"] == "replayed"


# ─── подмена: без сверки чужая личность проходит ───────────────────────────


class TestSubstitution:
    def test_disabling_the_check_lets_a_foreign_identity_through(self, solo, monkeypatch) -> None:
        """Узел ``claim_mismatch`` держит сверка, а не что-то соседнее: выключив
        её, та же чужая личность связывается с соло-профилем."""
        from users import specialist_identity_linking as mod

        monkeypatch.setattr(mod, "_refuse_unless_claim_matches", lambda *a, **k: None)
        proxy = _proxy(FOREIGN)

        response = _door_post(solo, FOREIGN)

        assert response.status_code == 201
        proxy.refresh_from_db()
        assert proxy.linked_user_id == solo.user_id


# ─── запасной ход и след ───────────────────────────────────────────────────


class TestOperatorFallbackAndLogs:
    def test_the_operator_bind_is_not_governed_by_the_door(self, solo) -> None:
        """Ручная связь оператором остаётся запасным ходом (fraud_suspected,
        duplicate_person): дверь её не заменяет и не ограничивает."""
        proxy = _proxy(FOREIGN)
        operator = User.objects.create_superuser(
            username="op-2450", password="pw", email="op-2450@x.y", role="admin",  # pragma: allowlist secret
        )

        bind_external_identity_by_operator(FOREIGN, solo.user_id, actor=operator)

        proxy.refresh_from_db()
        assert proxy.linked_user_id == solo.user_id

    def test_a_mismatch_logs_neither_the_claim_nor_the_presented_id(self, solo, caplog) -> None:
        with _capturing("users.specialist_identity_linking", caplog):
            with pytest.raises(SpecialistIdentityLinkRefused) as exc:
                link_specialist_identity(
                    solo.pk, FOREIGN, actor=ACTOR, idempotency_key="idem-2450-0009",
                )

        assert exc.value.reason == REASON_CLAIM_MISMATCH
        assert REASON_CLAIM_MISMATCH in caplog.text  # положительно: отказ записан
        assert OWN not in caplog.text
        assert FOREIGN not in caplog.text
