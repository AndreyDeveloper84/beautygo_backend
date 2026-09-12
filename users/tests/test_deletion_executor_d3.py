"""Исполнитель удаления аккаунта — срез D3 §7 (DRF-1699 / DRF-1725).

Пять сторожей из замера ``MEASUREMENT_DELETION_EXECUTOR_D3.md``:

1. полнота по перечислению — каждый указатель на ``User`` назван ровно в
   одной таблице исполнителя (новая FK без решения роняет тест);
2. полнота по данным — «человек со всем»: после прогона ни строки в
   У-моделях, ни личного значения в О-моделях, Х-строки на месте;
3. файлы — ``storage.exists`` после, не только строки;
4. откат при неполноте — ``FAILED``, ничего не доехало, заявка открыта;
5. нет ложного ``COMPLETED`` — бот не подтвердил → ``PROCESSING``, повтор.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from users.deletion_executor import (
    ANONYMISE,
    DELETE,
    ERASED_NAME,
    RETAIN,
    SCRUBBED,
    BotConfirmation,
    execute,
    pointers_to_user,
    scrub_personal_data,
    tombstone_user,
    undecided_pointers,
)
from users.deletion_requests import deletion_block_for, ensure_deletion_request
from users.models import (
    AnonymousSession,
    DeletionRequest,
    DeviceToken,
    FavoriteSpecialist,
    Profile,
    SocialAccount,
    SpecialistPortfolio,
    SpecialistProfile,
    TenantUserRelationship,
    User,
    UserPersonalContext,
)

pytestmark = pytest.mark.django_db

PHONE = "+79990601234"
FIRST, LAST = "Анастасия", "Петрова"
REVIEW_TEXT = f"Спасибо, {FIRST}! Звоните {PHONE}, почта a.petrova@example.com"


class _BotOk:
    """Бот подтвердил свою половину."""

    calls: list[dict]

    def __init__(self):
        self.calls = []

    def confirm(self, **kw):
        self.calls.append(kw)
        return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})


class _BotDown:
    def __init__(self, reason="bot_http_503"):
        self.reason = reason
        self.calls = 0

    def confirm(self, **kw):
        self.calls += 1
        return BotConfirmation(False, {}, self.reason)


# ---------------------------------------------------------------------------
# «Человек со всем»
# ---------------------------------------------------------------------------


@pytest.fixture
def master():
    u = User.objects.create_user(
        username="d3_master", password="pass",  # pragma: allowlist secret
        role="specialist", phone="+79990609999",
    )
    sp = u.specialist_profile
    sp.status = "active"
    sp.save()
    return u


@pytest.fixture
def service(master):
    from services.models import Service, ServiceCategory

    cat = ServiceCategory.objects.create(name="Ногти D3")
    return Service.objects.create(
        specialist=master.specialist_profile, category=cat,
        name="Маникюр", price=Decimal("1500"), duration_minutes=60,
    )


@pytest.fixture
def person(master, service):
    """Клиент, у которого есть строка в КАЖДОЙ У- и О-модели, файлы на
    носителе и прокси бота. Каждая строка объявлена здесь, чтобы «ноль
    после» нельзя было получить пустой фикстурой."""
    from ai.models import Conversation, Message
    from analytics.models import AnalyticsEvent
    from appointments.models import (
        Appointment,
        AppointmentRevision,
        IdempotencyKey,
        SpecialistTimeOff,
    )
    from billing.models import BillingConsent, SpecialistSubscription, TariffPlan
    from django.contrib.auth.models import Group, Permission
    from goals.models import ClientGoal, GoalAnketaAnswer, GoalAnketaRun
    from notifications.models import Notification
    from nutrition.models import (
        CrossDomainRule,
        CrossDomainShownRule,
        FoodLog,
        FoodScan,
        NutritionProfile,
        ProfileIdempotencyKey,
        WaterEntry,
        WaterLog,
    )
    from payments.models import UserPaymentMethod
    from reviews.models import Review
    from wellness.models import (
        DesiredOutcome,
        PersonalPlan,
        PlanOutcomeLink,
        ProgressObservation,
    )

    now = timezone.now()
    u = User.objects.create_user(
        username=f"user_{PHONE.lstrip('+')}", password="pass",  # pragma: allowlist secret
        role="client", phone=PHONE, email="a.petrova@example.com",
        first_name=FIRST, last_name=LAST,
    )
    tenant = master.specialist_profile.tenant
    u.tenant = tenant
    u.save()  # → TenantUserRelationship через сигнал
    assert TenantUserRelationship.objects.filter(user=u, is_active=True).exists()

    # профиль + аватар (файл)
    profile = u.profile
    profile.full_name = f"{FIRST} {LAST}"
    profile.bio = "люблю маникюр"
    profile.city = "Пенза"
    profile.default_location_lat = Decimal("53.2")
    profile.default_location_lng = Decimal("45.0")
    profile.avatar.save("a.jpg", ContentFile(b"x" * 10), save=False)
    profile.save()

    # мастер-профиль у того же человека (совмещённая роль) + портфолио
    sp = SpecialistProfile.objects.create(
        user=u, display_name=f"{FIRST} {LAST}", bio="bio", address="ул. Мира 1",
        location_lat=Decimal("53.2"), location_lng=Decimal("45.0"),
    )
    sp.avatar.save("s.jpg", ContentFile(b"y" * 10), save=True)
    item = SpecialistPortfolio(specialist=sp)
    item.image.save("p.jpg", ContentFile(b"z" * 10), save=True)

    # личность
    UserPersonalContext.objects.create(user=u, diet_type="vegan")
    SocialAccount.objects.create(user=u, provider="vk", provider_uid="123")
    DeviceToken.objects.create(user=u, token="tok-1", app_type="client", platform="ios")
    AnonymousSession.objects.create(
        device_id=uuid.uuid4(), user=u, platform="ios", expires_at=now + timedelta(days=1)
    )
    FavoriteSpecialist.objects.create(user=u, specialist=master.specialist_profile)
    u.groups.add(Group.objects.create(name="d3-group"))
    u.user_permissions.add(Permission.objects.first())
    RefreshToken.for_user(u)  # → OutstandingToken
    assert OutstandingToken.objects.filter(user=u).exists()

    # прокси бота
    User.objects.create(username="bot:max:d3-1", role="client", is_proxy=True, linked_user=u)

    # питание
    NutritionProfile.objects.create(
        user=u, weight_kg=60, height_cm=165, age=30, gender="female",
        activity_coefficient=1.6, goal="lose", daily_kcal=1800,
    )
    FoodLog.objects.create(
        user=u, dish_name="Борщ", meal_type="lunch", logged_at=now,
        idempotency_key=str(uuid.uuid4()),
    )
    scan = FoodScan.objects.create(user=u, dish_name="Борщ", provider_used=FoodScan.Provider.OPENAI)
    scan.image.save("scan.jpg", ContentFile(b"s" * 10), save=True)
    WaterEntry.objects.create(user=u, ts=now, ml=250, water_ml=250.0)
    WaterLog.objects.create(user=u, amount_ml=250, logged_at=now)
    rule = CrossDomainRule.objects.create(
        rule_id="d3_rule", nutrition_trigger="low_vitamin_d",
        service_category_slug="massage", insight_text_template="t",
        rationale_text="r", disclaimer_text="d", is_active=True, legal_reviewed=True,
    )
    CrossDomainShownRule.objects.create(
        user=u, rule=rule, nutrition_trigger="low_vitamin_d",
        service_category_slug="massage", shown_at=now, surface="bot",
    )
    ProfileIdempotencyKey.objects.create(
        key="d3-key", user=u, response={"x": 1}, expires_at=now + timedelta(hours=1)
    )

    # цели и планы
    goal = ClientGoal.objects.create(client=u, goal_key="relax", source_channel="bot")
    run = GoalAnketaRun.objects.create(client=u, goal=goal)
    GoalAnketaAnswer.objects.create(run=run, step_key="area", option_key="face")
    outcome = DesiredOutcome.objects.create(
        user=u, target="body_weight", statement_text="хочу сбросить вес",
        direction=DesiredOutcome.Direction.REDUCE, desired_state_numeric=Decimal("55"),
    )
    plan = PersonalPlan.objects.create(user=u)
    PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)
    first = ProgressObservation.objects.create(
        user=u, observation_type=ProgressObservation.ObservationType.WEIGHT,
        value_numeric=Decimal("60"), observed_at=now,
    )
    ProgressObservation.objects.create(
        user=u, observation_type=ProgressObservation.ObservationType.WEIGHT,
        value_numeric=Decimal("59"), observed_at=now, superseded_by=first,
    )

    # диалоги, уведомления, аналитика, кэш
    conv = Conversation.objects.create(user=u, tenant=tenant)
    Message.objects.create(conversation=conv, role=Message.Role.USER, content="привет")
    Notification.objects.create(
        user=u, template_id="t", channel=Notification.Channel.PUSH, title="t", body="b",
    )
    AnalyticsEvent.objects.create(
        event_name="app_opened", actor=u, app_type="client",
        client_event_id=uuid.uuid4(), client_timestamp=now,
    )
    IdempotencyKey.objects.create(
        user=u, key="k", operation_name="booking.create", target_id="x",
        request_body_hash="h", response_status=201, response_payload={},
        expires_at=now + timedelta(hours=1),
    )

    # сделки и деньги
    appt = Appointment.objects.create(
        client=u, specialist=master.specialist_profile, service=service,
        start_datetime=now - timedelta(hours=2), end_datetime=now - timedelta(hours=1),
        status="completed", price=service.price, notes="аллергия на лак",
    )
    cancelled = Appointment.objects.create(
        client=u, specialist=master.specialist_profile, service=service,
        start_datetime=now + timedelta(days=1), end_datetime=now + timedelta(days=1, hours=1),
        status="cancelled", price=service.price, cancelled_by=u,
    )
    AppointmentRevision.objects.create(
        appointment=cancelled, version=2, actor=u, actor_role="client",
        old_start_datetime=now, old_end_datetime=now, new_start_datetime=now, new_end_datetime=now,
    )
    SpecialistTimeOff.objects.create(
        specialist=sp, start_at=now, end_at=now + timedelta(hours=1), created_by=u,
    )
    Review.objects.create(
        appointment=appt, client=u, specialist=master.specialist_profile, service=service,
        rating=5, text=REVIEW_TEXT,
    )
    UserPaymentMethod.objects.create(
        user=u, payment_method_id="pm-d3-1", last4="4242", brand="Visa",
        consent_version="v1", consented_at=now,
    )
    SpecialistSubscription.objects.create(
        # get_or_create, не get: сид из миграции billing/0002 не переживает
        # flush транзакционных тестов при --reuse-db.
        user=u, tenant=tenant, tariff=TariffPlan.objects.get_or_create(
            code="solo", defaults={"name": "Solo", "price": Decimal("990"), "max_masters": 1},
        )[0],
        status="active", payment_method_id="pm-sub-1", card_brand="Visa",
        payment_method_saved_at=now,
    )
    BillingConsent.objects.create(user=u, document_version="offer-1.0")

    return u


def _files_of(user) -> list[tuple[object, str]]:
    from nutrition.models import FoodScan

    out = []
    p = Profile.objects.get(user=user)
    out.append((p.avatar.storage, p.avatar.name))
    sp = SpecialistProfile.objects.get(user=user)
    out.append((sp.avatar.storage, sp.avatar.name))
    for item in SpecialistPortfolio.objects.filter(specialist=sp):
        out.append((item.image.storage, item.image.name))
    for scan in FoodScan.objects.filter(user=user):
        out.append((scan.image.storage, scan.image.name))
    return out


# ---------------------------------------------------------------------------
# 1. Полнота по перечислению
# ---------------------------------------------------------------------------


class TestEveryPointerToUserIsDecided:
    def test_tables_cover_the_live_census_exactly(self):
        live = pointers_to_user()
        # Положительная стража: перепись не пуста и видит скрытые связи.
        assert len(live) >= 40, sorted(live)
        assert "services.SalonService.mapping_confirmed_by" in live  # related_name="+"
        assert "users.User_groups.user" in live  # служебная through-таблица
        assert undecided_pointers() == {}, undecided_pointers()

    def test_each_key_is_in_exactly_one_table(self):
        keys = [*DELETE, *ANONYMISE, *RETAIN]
        assert len(keys) == len(set(keys))
        assert set(keys) == pointers_to_user()

    def test_a_new_pointer_without_a_decision_is_reported(self):
        with patch("users.deletion_executor.pointers_to_user", return_value=pointers_to_user() | {"x.New.user"}):
            assert undecided_pointers() == {"missing": ["x.New.user"]}

    def test_an_undecided_pointer_stops_the_executor_before_any_write(self, person):
        req = ensure_deletion_request(person, initiator="bot").request
        with patch("users.deletion_executor.pointers_to_user", return_value=pointers_to_user() | {"x.New.user"}):
            out = execute(req, bot_client=_BotOk())
        req.refresh_from_db()
        assert out.status == req.status == DeletionRequest.Status.FAILED
        assert "x.New.user" in req.failure_reason
        person.refresh_from_db()
        assert person.phone == PHONE  # ничего не тронуто


# ---------------------------------------------------------------------------
# 2–3. Полнота по данным и файлы
# ---------------------------------------------------------------------------


class TestPersonWithEverythingIsErased:
    def test_delete_anonymise_retain(self, person, master):
        from analytics.models import AnalyticsEvent
        from appointments.models import Appointment, AppointmentRevision, SpecialistTimeOff
        from billing.models import BillingConsent, SpecialistSubscription
        from reviews.models import Review

        files = _files_of(person)
        assert len(files) == 4 and all(st.exists(n) for st, n in files)
        req = ensure_deletion_request(person, initiator="bot").request
        assert deletion_block_for(person) is not None
        bot = _BotOk()

        out = execute(req, bot_client=bot)

        req.refresh_from_db()
        assert out.completed and req.status == DeletionRequest.Status.COMPLETED
        assert req.completed_at is not None and req.started_at is not None
        assert deletion_block_for(person) is None  # гейт D2 открылся тем же ходом

        # У — ни строки. Перечислением по таблице, не «по ощущению».
        for key in DELETE:
            label, fname = key.rsplit(".", 1)
            if label in ("users.User_groups", "users.User_user_permissions"):
                continue
            from django.apps import apps as django_apps

            app_label, model_name = label.split(".")
            model = django_apps.get_model(app_label, model_name)
            manager = getattr(model, "all_objects", model.objects)
            assert manager.filter(**{fname: person}).count() == 0, key
        assert person.groups.count() == 0 and person.user_permissions.count() == 0
        assert OutstandingToken.objects.filter(user=person).count() == 0

        # файлы — с носителя, не только строки
        assert not any(st.exists(n) for st, n in files)
        assert out.steps["files_deleted"] == 4

        # О — строки на месте, личного нет
        person.refresh_from_db()
        assert person.phone is None and person.email == ""
        assert (person.first_name, person.last_name) == (ERASED_NAME, "")
        assert person.username == f"deleted:{person.pk}"
        assert not person.is_active and person.deleted_at is not None
        p = Profile.objects.get(user=person)
        assert (p.full_name, p.bio, p.city) == (ERASED_NAME, "", "")
        assert not p.avatar and p.default_location_lat is None
        sp = SpecialistProfile.objects.get(user=person)
        assert (sp.display_name, sp.bio, sp.address) == (ERASED_NAME, "", "")
        assert not sp.avatar and sp.location_lat is None and not sp.is_available
        tomb = tombstone_user()
        appts = Appointment.objects.filter(client=tomb)
        assert appts.count() == 2 and not appts.exclude(notes="").exists()
        assert Appointment.objects.filter(client=person).count() == 0
        assert Appointment.objects.filter(cancelled_by=person).count() == 0
        assert AppointmentRevision.objects.filter(actor=person).count() == 0
        assert SpecialistTimeOff.objects.filter(created_by=person).count() == 0
        review = Review.objects.get(client=tomb)
        assert review.is_anonymous and review.rating == 5
        assert FIRST not in review.text and PHONE not in review.text and "example.com" not in review.text
        assert "Спасибо" in review.text  # текст остался (D9)
        sub = SpecialistSubscription.objects.get(tenant=master.specialist_profile.tenant)
        assert sub.user_id == person.pk and sub.status == "active"  # хранить (D8)
        assert sub.payment_method_id == "" and sub.card_brand == ""  # способ оплаты — сразу
        assert BillingConsent.objects.get(user=person).revoked_at is not None
        tur = TenantUserRelationship.objects.get(user=person)
        assert not tur.is_active and tur.revoke_reason == "account_deleted"
        assert AnalyticsEvent.objects.filter(actor=person).count() == 0
        assert User.objects.filter(is_proxy=True, linked_user=person).count() == 0

        # Х — заявка жива и на человеке
        assert DeletionRequest.objects.filter(user=person).count() == 1
        assert req.steps["retained"] == RETAIN

        # бот спрошен один раз, с прокси ещё привязанным
        assert len(bot.calls) == 1
        assert bot.calls[0]["external_user_ids"] == ["bot:max:d3-1"]
        assert bot.calls[0]["ayla_user_id"] == str(person.pk)

    def test_rerun_of_a_completed_request_is_a_noop(self, person):
        req = ensure_deletion_request(person, initiator="bot").request
        execute(req, bot_client=_BotOk())
        bot = _BotOk()
        out = execute(req, bot_client=bot)
        assert out.completed and bot.calls == []

    def test_a_person_with_nothing_completes_too(self):
        u = User.objects.create_user(username="d3_empty", password="pass", role="client")  # pragma: allowlist secret
        req = ensure_deletion_request(u, initiator="app").request
        out = execute(req, bot_client=_BotOk())
        assert out.completed
        u.refresh_from_db()
        assert u.deleted_at is not None and u.username == f"deleted:{u.pk}"


class TestScrub:
    def test_phone_email_and_name_are_hidden_text_stays(self):
        out = scrub_personal_data(REVIEW_TEXT, [FIRST, LAST])
        assert out == f"Спасибо, {SCRUBBED}! Звоните {SCRUBBED}, почта {SCRUBBED}"

    def test_short_or_erased_names_do_not_scrub_ordinary_words(self):
        assert scrub_personal_data("Ок, всё хорошо", ["Ок", ERASED_NAME]) == "Ок, всё хорошо"


# ---------------------------------------------------------------------------
# 4. Откат при неполноте
# ---------------------------------------------------------------------------


class TestIncompleteErasureRollsBack:
    def test_residue_means_failed_and_nothing_written(self, person):
        from nutrition.models import FoodLog

        req = ensure_deletion_request(person, initiator="bot").request
        files = _files_of(person)
        real = FoodLog.objects.filter(user=person).count()
        assert real == 1

        # Подмена: один шаг «забыт» — дневник не удалён. Полнота обязана это
        # увидеть по перечитанной строке и откатить всё.
        with patch("users.deletion_executor._residue", return_value={"nutrition.FoodLog": 1}):
            bot = _BotOk()
            out = execute(req, bot_client=bot)

        req.refresh_from_db()
        assert out.status == req.status == DeletionRequest.Status.FAILED
        assert "nutrition.FoodLog" in req.failure_reason
        assert req.is_open and deletion_block_for(person) is not None
        assert bot.calls == []  # до бота не дошли
        person.refresh_from_db()
        assert person.phone == PHONE and person.first_name == FIRST
        assert FoodLog.objects.filter(user=person).count() == 1
        assert Profile.objects.get(user=person).full_name == f"{FIRST} {LAST}"
        # Файлы сняты ДО отката — это единственный след, и повтор его терпит.
        assert not any(st.exists(n) for st, n in files)

        out2 = execute(req, bot_client=_BotOk())
        assert out2.completed

    def test_a_file_that_survives_delete_is_incomplete(self, person):
        req = ensure_deletion_request(person, initiator="bot").request
        with patch("django.core.files.storage.FileSystemStorage.delete", lambda self, name: None):
            out = execute(req, bot_client=_BotOk())
        assert out.status == DeletionRequest.Status.FAILED
        assert "file still present" in out.failure_reason


# ---------------------------------------------------------------------------
# 5. Нет ложного COMPLETED
# ---------------------------------------------------------------------------


class TestBotMustConfirm:
    def test_bot_5xx_leaves_processing_and_retries_only_the_bot(self, person):
        req = ensure_deletion_request(person, initiator="bot").request
        down = _BotDown("bot_http_503")

        out = execute(req, bot_client=down)

        req.refresh_from_db()
        assert out.status == req.status == DeletionRequest.Status.PROCESSING
        assert req.failure_reason == "bot_http_503"
        assert req.completed_at is None and req.is_open
        assert deletion_block_for(person) is not None
        # каталог стёрт, прокси ещё привязан — боту нужен субъект
        person.refresh_from_db()
        assert person.phone is None
        assert User.objects.filter(is_proxy=True, linked_user=person).count() == 1

        ok = _BotOk()
        out2 = execute(req, bot_client=ok)
        req.refresh_from_db()
        assert out2.completed and req.status == DeletionRequest.Status.COMPLETED
        assert req.failure_reason == "" and len(ok.calls) == 1
        assert User.objects.filter(is_proxy=True, linked_user=person).count() == 0

    def test_bot_200_without_all_ok_is_not_a_confirmation(self, person):
        req = ensure_deletion_request(person, initiator="bot").request

        class _NotOk:
            def confirm(self, **kw):
                return BotConfirmation(False, {"all_ok": False, "failed_steps": ["ayla_delete"]},
                                       "bot_not_ok: ['ayla_delete']")

        out = execute(req, bot_client=_NotOk())
        req.refresh_from_db()
        assert out.status == req.status == DeletionRequest.Status.PROCESSING
        assert "ayla_delete" in req.failure_reason
        assert req.steps["bot"] == {"all_ok": False, "failed_steps": ["ayla_delete"]}

    def test_unset_bot_url_is_unreachable_not_completed(self, person, settings):
        from users.deletion_executor import BotDeletionClient

        settings.BOT_PLATFORM_BASE_URL = ""
        req = ensure_deletion_request(person, initiator="bot").request
        out = execute(req, bot_client=BotDeletionClient())
        assert out.status == DeletionRequest.Status.PROCESSING
        assert out.failure_reason.startswith("bot_unreachable")


class TestBotClientWire:
    def test_signed_post_and_response_shapes(self, settings):
        import json

        from users.deletion_executor import BotDeletionClient

        settings.BOT_PLATFORM_BASE_URL = "https://bot.test.local"
        settings.AYLA_OUTBOUND_HMAC_SECRET = "s3cret"  # pragma: allowlist secret
        seen = {}

        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._p = payload

            def json(self):
                return self._p

        def fake_post(url, *, content, headers, timeout):
            seen.update(url=url, content=content, headers=headers)
            return _Resp(200, {"data": {"all_ok": True, "steps": [], "flag_cleared": False}})

        with patch("users.deletion_executor.httpx.post", fake_post):
            out = BotDeletionClient().confirm(
                request_id="r1", ayla_user_id="u1", external_user_ids=["bot:max:1"]
            )
        assert out.ok
        assert seen["url"] == "https://bot.test.local/api/v1/internal/privacy/account-deletion/"
        assert json.loads(seen["content"]) == {
            "ayla_user_id": "u1", "external_user_ids": ["bot:max:1"], "request_id": "r1",
        }
        assert seen["headers"]["X-Ayla-Event-Signature"].startswith("sha256=")
        assert seen["headers"]["X-Ayla-Event-Timestamp"].isdigit()
        assert seen["headers"]["X-Idempotency-Key"] == "r1"

        with patch("users.deletion_executor.httpx.post", lambda *a, **k: _Resp(503, {})):
            assert BotDeletionClient().confirm(
                request_id="r1", ayla_user_id="u1", external_user_ids=[]
            ).reason == "bot_http_503"


# ---------------------------------------------------------------------------
# Тик Celery
# ---------------------------------------------------------------------------


def _age(req, days):
    """Заявке ``days`` дней: ``requested_at`` не auto_now, но фикстура
    ставит «сейчас» — состариваем по pk."""
    DeletionRequest.objects.filter(pk=req.pk).update(
        requested_at=timezone.now() - timedelta(days=days)
    )
    req.refresh_from_db()
    return req


class TestGraceWindow:
    """§7: крайняя дата — через 30 дней; нажатие ≠ стирание через 15 минут."""

    def test_a_fresh_request_is_not_due(self, person):
        from users.deletion_executor import open_requests_due
        from users.tasks import execute_deletion_requests

        req = ensure_deletion_request(person, initiator="bot").request
        assert req.is_open  # положительная пара: заявка живая
        assert list(open_requests_due()) == []
        with patch("users.deletion_executor.BotDeletionClient", _BotOk):
            counters = execute_deletion_requests()
        assert counters == {"scanned": 0, "completed": 0, "open": 0}
        req.refresh_from_db()
        person.refresh_from_db()
        assert req.status == DeletionRequest.Status.REQUESTED and person.phone == PHONE

    def test_a_request_past_the_window_is_due_and_executed(self, person):
        from users.deletion_executor import open_requests_due
        from users.tasks import execute_deletion_requests

        req = _age(ensure_deletion_request(person, initiator="bot").request, 30)
        assert [r.pk for r in open_requests_due()] == [req.pk]
        with patch("users.deletion_executor.BotDeletionClient", _BotOk):
            counters = execute_deletion_requests()
        assert counters == {"scanned": 1, "completed": 1, "open": 0}
        req.refresh_from_db()
        assert req.status == DeletionRequest.Status.COMPLETED

    def test_window_is_a_setting_with_a_default_of_30(self, person, settings):
        from users.deletion_executor import deletion_grace, open_requests_due

        delattr(settings, "DELETION_GRACE_DAYS")
        assert deletion_grace() == timedelta(days=30)
        req = _age(ensure_deletion_request(person, initiator="bot").request, 29)
        assert list(open_requests_due()) == []
        settings.DELETION_GRACE_DAYS = "7"
        assert [r.pk for r in open_requests_due()] == [req.pk]
        settings.DELETION_GRACE_DAYS = 0
        assert [r.pk for r in open_requests_due()] == [req.pk]

    @pytest.mark.parametrize("bad", ["", "месяц", -1, None])
    def test_misconfigured_window_takes_nobody(self, person, settings, bad):
        from users.deletion_executor import GraceMisconfigured, open_requests_due
        from users.tasks import execute_deletion_requests

        _age(ensure_deletion_request(person, initiator="bot").request, 400)
        settings.DELETION_GRACE_DAYS = bad
        with pytest.raises(GraceMisconfigured):
            list(open_requests_due())
        with patch("users.deletion_executor.BotDeletionClient", _BotOk):
            counters = execute_deletion_requests()
        assert counters == {"scanned": 0, "completed": 0, "open": 0}
        person.refresh_from_db()
        assert person.phone == PHONE


class TestTick:
    def test_tick_executes_open_requests_and_counts(self, person):
        from users.tasks import execute_deletion_requests

        req = _age(ensure_deletion_request(person, initiator="bot").request, 31)
        with patch("users.deletion_executor.BotDeletionClient", _BotOk):
            counters = execute_deletion_requests()
        assert counters == {"scanned": 1, "completed": 1, "open": 0}
        req.refresh_from_db()
        assert req.status == DeletionRequest.Status.COMPLETED

    def test_tick_keeps_going_past_a_crash(self, person):
        from users.tasks import execute_deletion_requests

        other = User.objects.create_user(
            username="d3_other", password="pass", role="client",  # pragma: allowlist secret
        )
        _age(ensure_deletion_request(person, initiator="bot").request, 31)
        _age(ensure_deletion_request(other, initiator="bot").request, 31)

        real_execute = execute
        calls = []

        def boom(req, **kw):
            calls.append(req.pk)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real_execute(req, bot_client=_BotOk())

        with patch("users.deletion_executor.execute", boom):
            counters = execute_deletion_requests()
        assert counters == {"scanned": 2, "completed": 1, "open": 1}


class TestAdminExecuteNow:
    def test_action_queues_only_open_requests(self, person, admin_client):
        from django.urls import reverse

        open_req = ensure_deletion_request(person, initiator="bot").request
        done = User.objects.create_user(
            username="d3_done", password="pass", role="client",  # pragma: allowlist secret
        )
        done_req = ensure_deletion_request(done, initiator="app").request
        DeletionRequest.objects.filter(pk=done_req.pk).update(
            status=DeletionRequest.Status.COMPLETED, completed_at=timezone.now()
        )

        with patch("users.tasks.execute_deletion_request.delay") as delay:
            resp = admin_client.post(
                reverse("admin:users_deletionrequest_changelist"),
                {"action": "execute_now", "_selected_action": [str(open_req.pk), str(done_req.pk)]},
                follow=True,
            )
        assert resp.status_code == 200
        delay.assert_called_once_with(str(open_req.pk))
        assert "Поставлено в очередь исполнителя: 1." in resp.content.decode()
