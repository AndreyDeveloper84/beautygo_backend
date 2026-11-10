"""Удаление аккаунта соло-мастера закрывает его workspace и снимает профиль с проверки.

До правки (живой прогон 09.11.2026 на пилоте): после удаления аккаунта
соло-workspace обезличен (DRF-1935), но жив — ``is_active=True``; профиль
«Удалён» без фото стоял в очереди модератора в статусе «на верификации», и
действие «Подтвердить» предлагало его одобрить.

Решение владельца 10.11.2026 (вместе с закрытием кабинета в боте): workspace,
в котором мастер один, закрывается — ``Tenant.is_active=False``, строка
остаётся (записи D7, подписка, общий с ботом UUID); профиль на проверке или
опубликованный возвращается в черновик. Бот заводит вернувшемуся человеку новый
кабинет, закрытый остаётся надгробием.

Узлы:

* w1 — соло-workspace мастера-одиночки закрыт; имя «Студия» как прежде; шаг называет;
* w2 — соло-workspace с другим живым мастером — не закрыт (прежнее ``solo_shared``);
* w3 — салон — не трогается;
* w4 — профиль на верификации → черновик, шаг называет; опубликованный → черновик;
  черновик остаётся черновиком, и шаг этого не называет;
* w5 — закрытый workspace не виден обычному менеджеру и виден ``all_objects``;
* w6 — повтор исполнителя по завершённой заявке ничего не меняет (как и прежде);
* w7 — бот отказал на первом проходе, подтвердил на втором: закрытие и черновик
  сделаны первым проходом, второй данных не меняет и ключей закрытия не пишет;
* w8 — подписка закрытого workspace отменена (идемпотентно, как кнопка «отменить»),
  шаг называет; у workspace с другим живым мастером — не тронута; подписка,
  которую платит другой человек, — не тронута (его согласия на списание чужое
  удаление не снимает), названа в ``kept``;
* w9 — модератор: «Подтвердить мастеров» не берёт профиль удалённого аккаунта —
  и салонного, где гейт готовности соло не действует;
* w10 — остаток: открытый workspace или профиль не в черновике после стирания —
  ``IncompleteErasure``, заявка ``FAILED``, каталожная половина откачена.

«Красный до правки» у w1/w4/w8/w9 — по существу; кроме того, файл не
импортируется без новых констант исполнителя — это тоже красный, но не он
здесь главное.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib import admin as django_admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory

from billing.models import SpecialistSubscription, TariffPlan
from tenants.models import Tenant
from users.admin import SpecialistProfileAdmin, approve_specialists
from users.deletion_executor import (
    SOLO_SUBSCRIPTION_CANCELED_KEY,
    SOLO_SUBSCRIPTION_OTHER_PAYER_KEY,
    SOLO_TENANT_CLOSED_KEY,
    SPECIALIST_PUBLICATION_WITHDRAWN_KEY,
    BotConfirmation,
    execute,
)
from users.deletion_requests import ensure_deletion_request
from users.models import DeletionRequest, SpecialistProfile, User

pytestmark = pytest.mark.django_db


class _BotOk:
    def confirm(self, **kw):
        return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})


class _BotDown:
    def confirm(self, **kw):
        return BotConfirmation(False, {}, "bot_http_503")


def _subscription(user: User, tenant: Tenant, *, status: str = "active") -> SpecialistSubscription:
    return SpecialistSubscription.objects.create(
        user=user, tenant=tenant, status=status,
        tariff=TariffPlan.objects.get_or_create(
            code="solo", defaults={"name": "Solo", "price": Decimal("990"), "max_masters": 1},
        )[0],
    )


def _tenant(slug: str, kind: str) -> Tenant:
    return Tenant.objects.create(
        slug=slug, name="Студия Веры", kind=kind, address="ул. Садовая, 5", city="Пенза",
        latitude=Decimal("53.100000"), longitude=Decimal("45.100000"),
    )


def _master(username: str, *, tenant: Tenant, status: str | None = None) -> User:
    u = User.objects.create_user(
        username=username, password="pass",  # pragma: allowlist secret
        role="specialist",
    )
    sp = u.specialist_profile
    sp.display_name = "Вера Мастерова"
    sp.tenant = tenant
    if status is not None:
        sp.status = status
    sp.save()
    return u


def _delete(user: User):
    req = ensure_deletion_request(user, initiator="bot").request
    out = execute(req, bot_client=_BotOk())
    assert out.completed, out
    req.refresh_from_db()
    return req


def _profile(user: User) -> SpecialistProfile:
    return SpecialistProfile.objects.get(user=user)


def test_w1_a_lone_solo_workspace_is_closed_with_the_account():
    """Красный до правки: тенант оставался ``is_active=True``."""
    solo = _tenant("close-w1", Tenant.Kind.SOLO)
    u = _master("close-w1", tenant=solo)

    req = _delete(u)

    t = Tenant.all_objects.get(pk=solo.pk)
    assert t.is_active is False
    assert t.name == "Студия"  # обезличивание DRF-1935 — как прежде
    assert req.steps["anonymised"]["tenants.Tenant.solo"] == 1
    assert req.steps["anonymised"][SOLO_TENANT_CLOSED_KEY] == 1


def test_w2_a_solo_workspace_shared_with_a_live_master_stays_open():
    solo = _tenant("close-w2", Tenant.Kind.SOLO)
    leaving = _master("close-w2-a", tenant=solo)
    _master("close-w2-b", tenant=solo)

    req = _delete(leaving)

    t = Tenant.all_objects.get(pk=solo.pk)
    assert t.is_active is True
    assert t.name == "Студия Веры"
    assert req.steps["kept"]["tenants.Tenant.solo_shared"] == 1
    assert SOLO_TENANT_CLOSED_KEY not in req.steps["anonymised"]


def test_w3_a_salon_is_untouched():
    salon = _tenant("close-w3", Tenant.Kind.SALON)
    u = _master("close-w3", tenant=salon)

    req = _delete(u)

    t = Tenant.all_objects.get(pk=salon.pk)
    assert t.is_active is True
    assert t.name == "Студия Веры"
    assert SOLO_TENANT_CLOSED_KEY not in req.steps["anonymised"]


@pytest.mark.parametrize(
    ("status", "withdrawn"),
    [
        (SpecialistProfile.ProfileStatus.PENDING, True),
        (SpecialistProfile.ProfileStatus.ACTIVE, True),
        (SpecialistProfile.ProfileStatus.DRAFT, False),
    ],
    ids=["pending", "active", "draft"],
)
def test_w4_a_deleted_profile_is_withdrawn_to_draft(status: str, withdrawn: bool):
    """Красный до правки для pending/active: профиль оставался в очереди модератора / в витрине."""
    solo = _tenant(f"close-w4-{status}", Tenant.Kind.SOLO)
    u = _master(f"close-w4-{status}", tenant=solo, status=status)
    assert _profile(u).status == status  # исходное состояние действительно выставлено

    req = _delete(u)

    assert _profile(u).status == SpecialistProfile.ProfileStatus.DRAFT
    assert (SPECIALIST_PUBLICATION_WITHDRAWN_KEY in req.steps["anonymised"]) is withdrawn
    assert _profile(u).display_name == "Удалён"  # обезличивание — как прежде


def test_w5_a_closed_workspace_is_hidden_from_the_default_manager():
    solo = _tenant("close-w5", Tenant.Kind.SOLO)
    u = _master("close-w5", tenant=solo)
    assert Tenant.objects.filter(pk=solo.pk).exists()

    _delete(u)

    assert Tenant.all_objects.filter(pk=solo.pk).exists()
    assert not Tenant.objects.filter(pk=solo.pk).exists()


def test_w6_a_rerun_of_the_completed_request_changes_nothing():
    solo = _tenant("close-w6", Tenant.Kind.SOLO)
    u = _master("close-w6", tenant=solo, status=SpecialistProfile.ProfileStatus.PENDING)
    req = _delete(u)
    before = Tenant.all_objects.get(pk=solo.pk).updated_at

    again = execute(req, bot_client=_BotOk())

    assert again.completed
    assert Tenant.all_objects.get(pk=solo.pk).updated_at == before
    assert _profile(u).status == SpecialistProfile.ProfileStatus.DRAFT


def test_w7_a_retry_after_the_bot_refused_keeps_the_closure_and_changes_no_data():
    solo = _tenant("close-w7", Tenant.Kind.SOLO)
    u = _master("close-w7", tenant=solo, status=SpecialistProfile.ProfileStatus.PENDING)
    req = ensure_deletion_request(u, initiator="bot").request

    first = execute(req, bot_client=_BotDown())
    req.refresh_from_db()
    assert not first.completed
    assert req.status == DeletionRequest.Status.PROCESSING
    # Каталожная половина прошла: закрыто и снято уже на первом проходе.
    assert Tenant.all_objects.get(pk=solo.pk).is_active is False
    assert _profile(u).status == SpecialistProfile.ProfileStatus.DRAFT
    assert req.steps["anonymised"][SOLO_TENANT_CLOSED_KEY] == 1
    assert req.steps["anonymised"][SPECIALIST_PUBLICATION_WITHDRAWN_KEY] == 1

    second = execute(req, bot_client=_BotOk())

    req.refresh_from_db()
    assert second.completed
    assert req.status == DeletionRequest.Status.COMPLETED
    assert Tenant.all_objects.get(pk=solo.pk).is_active is False
    assert _profile(u).status == SpecialistProfile.ProfileStatus.DRAFT
    # Второй проход застал уже закрытый workspace и черновик — ключей закрытия
    # не пишет; «обезличено» пересохраняется и называется как прежде.
    assert "tenants.Tenant.solo" in req.steps["anonymised"]
    assert SOLO_TENANT_CLOSED_KEY not in req.steps["anonymised"]
    assert SPECIALIST_PUBLICATION_WITHDRAWN_KEY not in req.steps["anonymised"]


def test_w8_the_closed_workspaces_subscription_is_canceled():
    """Красный до правки: подписка жила ``active`` при аккаунте, которого нет."""
    solo = _tenant("close-w8", Tenant.Kind.SOLO)
    u = _master("close-w8", tenant=solo)
    sub = _subscription(u, solo)

    req = _delete(u)

    sub.refresh_from_db()
    assert sub.status == SpecialistSubscription.Status.CANCELED
    assert sub.canceled_at is not None
    assert req.steps["anonymised"][SOLO_SUBSCRIPTION_CANCELED_KEY] == 1


def test_w8_a_shared_workspaces_subscription_is_untouched():
    solo = _tenant("close-w8s", Tenant.Kind.SOLO)
    leaving = _master("close-w8s-a", tenant=solo)
    staying = _master("close-w8s-b", tenant=solo)
    sub = _subscription(staying, solo)

    req = _delete(leaving)

    sub.refresh_from_db()
    assert sub.status == SpecialistSubscription.Status.ACTIVE
    assert SOLO_SUBSCRIPTION_CANCELED_KEY not in req.steps["anonymised"]


def test_w8_a_subscription_paid_by_someone_else_is_left_alone():
    """``cancel_subscription`` снимает все согласия плательщика — чужих согласий
    удаление этого аккаунта касаться не должно."""
    from billing.models import BillingConsent

    solo = _tenant("close-w8p", Tenant.Kind.SOLO)
    u = _master("close-w8p", tenant=solo)
    payer = User.objects.create_user(
        username="close-w8p-payer", password="pass", role="admin",  # pragma: allowlist secret
    )
    sub = _subscription(payer, solo)
    consent = BillingConsent.objects.create(user=payer, document_version="offer-test")
    assert consent.revoked_at is None

    req = _delete(u)

    sub.refresh_from_db()
    consent.refresh_from_db()
    assert sub.status == SpecialistSubscription.Status.ACTIVE
    assert consent.revoked_at is None
    assert Tenant.all_objects.get(pk=solo.pk).is_active is False
    assert req.steps["kept"][SOLO_SUBSCRIPTION_OTHER_PAYER_KEY] == 1
    assert SOLO_SUBSCRIPTION_CANCELED_KEY not in req.steps["anonymised"]


def _approve(queryset, operator: User) -> list[str]:
    request = RequestFactory().post("/admin/users/specialistprofile/")
    request.user = operator
    request.session = {}
    request._messages = FallbackStorage(request)
    approve_specialists(SpecialistProfileAdmin(SpecialistProfile, django_admin.site), request, queryset)
    return [str(m) for m in request._messages]


def test_w9_the_moderator_cannot_approve_a_deleted_salon_master():
    """Красный до правки: у салонного мастера гейт готовности соло не действует —
    одно нажатие публиковало «Удалён»."""
    salon = _tenant("close-w9", Tenant.Kind.SALON)
    u = _master("close-w9", tenant=salon, status=SpecialistProfile.ProfileStatus.PENDING)
    _delete(u)
    assert _profile(u).status == SpecialistProfile.ProfileStatus.DRAFT
    operator = User.objects.create_superuser(
        username="op-close-w9", password="pw", email="op-w9@x.y", role="admin",  # pragma: allowlist secret
    )

    messages = _approve(SpecialistProfile.objects.filter(pk=_profile(u).pk), operator)

    assert any("Подтверждено мастеров: 0" in m for m in messages), messages
    assert _profile(u).status == SpecialistProfile.ProfileStatus.DRAFT


@pytest.mark.parametrize("leave", ["open", "status"], ids=["workspace-open", "profile-pending"])
def test_w10_a_half_done_erasure_is_caught_by_the_residue_check(monkeypatch, leave: str):
    """Полнота — по перечитанным строкам: стирание, не доведённое до конца, —
    ``FAILED`` и откат каталожной половины."""
    from users import deletion_executor as executor

    solo = _tenant(f"close-w10-{leave}", Tenant.Kind.SOLO)
    u = _master(f"close-w10-{leave}", tenant=solo, status=SpecialistProfile.ProfileStatus.PENDING)
    real_residue = executor._residue

    def leave_something_and_measure(user, *args, **kw):
        if leave == "open":
            Tenant.all_objects.filter(pk=solo.pk).update(is_active=True)
        else:
            SpecialistProfile.objects.filter(user=user).update(
                status=SpecialistProfile.ProfileStatus.PENDING
            )
        return real_residue(user, *args, **kw)

    monkeypatch.setattr(executor, "_residue", leave_something_and_measure)
    req = ensure_deletion_request(u, initiator="bot").request

    out = execute(req, bot_client=_BotOk())

    req.refresh_from_db()
    assert not out.completed
    assert req.status == DeletionRequest.Status.FAILED
    expected_key = "tenants.Tenant.solo_open" if leave == "open" else "users.SpecialistProfile.publication"
    assert expected_key in (req.failure_reason or ""), req.failure_reason
    # Откат: каталожная половина не записана.
    assert _profile(u).display_name == "Вера Мастерова"
    assert Tenant.all_objects.get(pk=solo.pk).name == "Студия Веры"
