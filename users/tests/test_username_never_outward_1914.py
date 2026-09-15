"""``username`` не уходит наружу как имя человека (DRF-1914).

Перепись 15.09 (каталог 5501257): 36 чтений ``username`` в не-тестовом коде,
наружу — три места: публичный список отзывов и ответ отзыва подставляли
``username`` вместо пустого имени, сигнал создания мастера клал ``username``
в ``SpecialistProfile.display_name`` (имя на витрине). Формы ``username``:
``bot:max:<id>`` (прокси бота), ``user_<цифры телефона>`` (регистрация по
телефону), ``social_<provider>_<uid>``, ``anon_<id>``, ``deleted:<pk>``.

Замер пилота главным окном (ruvds-o1mqo, 5501257, 15.09 04:10 UTC): механизм
открыт; экспозиция — 0 отзывов, 0 из 31 мастеров с display_name от username.

Что заперто:

* **класс**: в не-тестовом коде ``username`` читается только в названных
  местах, у каждого причина; сторож проверен на себе и не пуст (нижняя граница
  по числу разрешённых мест, которые он обязан найти);
* **отзывы**: публичный GET и ответ отзыва — имя, иначе «Клиент», анонимный —
  без имени; в теле ответа нет ни ``username``, ни цифр из него;
* **мастер**: ``User(role=specialist)`` с ``username`` ``user_<телефон>`` —
  ``display_name`` без цифр телефона; с именем — имя.

Предел: сторож читает написание. ``username``, прочитанный через переменную
(``getattr(user, field)``) или сериализатор с ``fields = '__all__'`` на
``User``, ему не виден; поведенческие тесты держат три известные поверхности.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import Appointment
from reviews.models import Review
from reviews.serializers import ReviewDetailSerializer
from services.models import Service, ServiceCategory
from users.models import User
from users.public_name import CLIENT_LABEL, MASTER_LABEL

REPO = Path(__file__).resolve().parents[2]
READ = re.compile(
    r"\.username\b(?!\s*=[^=])"
    r"|getattr\([^)]*['\"]username['\"]"
    r"|['\"]username['\"]\s*[,\]\)]"
    r"|source\s*=\s*['\"][\w.]*username"
)
NOT_A_READ = re.compile(r"username\s*=\s*f?['\"]|filter\(|exclude\(|order_by\(|get\(username")
SKIP_PARTS = {"tests", "migrations", "venv", ".venv", "node_modules", "seeds", "__pycache__"}

#: Where ``username`` may be read, each with its reason. None of these renders it
#: as a person's name on a client, master or public surface.
ALLOWED = {
    "users/admin.py": "Django admin search field — operators only",
    "users/admin_actions.py": "admin list of pending external identities: the proxy username IS the external id",
    "users/models.py": "__str__ for admin and logs",
    "users/account_reset.py": "operator reset of a bot identity: parses bot:<channel>:<id>",
    "users/deletion_executor.py": "erasure: overwrites username and lists the identities it unbinds",
    "users/identity_card.py": "operator identity card: proxy id as typed by the operator, real account masked",
    "users/personal_data_api.py": "personal-data export to the subject: their own external_user_id (s2s)",
    "users/management/commands/reset_test_account.py": "operator command output, masked for real accounts",
    "nutrition/services/water_entry_service.py": "s2s outbox: the proxy username is the bot's external id",
    "nutrition/services/outbox_service.py": "docstring on the same s2s external id",
    "nutrition/services/cross_domain_engine.py": "internal allowlist match on the external id",
    "services/mapping_review.py": "operator audit line: who confirmed the mapping (staff)",
    "services/management/commands/verify_pilot_slice.py": "operator command output: confirming staff account",
    "services/management/commands/seed_demo_salons.py": "demo seed input dict",
    "tenants/management/commands/promote_tenant_location.py": "operator command output: confirming staff account",
}

#: Surfaces that used to leak and must now stay out of the census.
FIXED = ["reviews/serializers.py", "users/signals.py"]


def _reads() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if set(rel.split("/")) & SKIP_PARTS:
            continue
        lines = [
            n for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)
            if READ.search(line) and not NOT_A_READ.search(line)
        ]
        if lines:
            out[rel] = lines
    return out


class TestTheCensus:
    def test_the_pattern_catches_every_spelling_it_names(self):
        for sample in (
            "return full or user.username",
            'getattr(user, "username", None)',
            'User.objects.only("username", "is_proxy")',
            "client_name = serializers.CharField(source='client.username')",
        ):
            assert READ.search(sample) and not NOT_A_READ.search(sample), sample
        for sample in ('username=f"user_{phone}"', 'user.username = f"deleted:{pk}"', 'filter(username=x)'):
            assert not (READ.search(sample) and not NOT_A_READ.search(sample)), sample

    def test_the_scan_is_not_vacuous(self):
        reads = _reads()
        found = [rel for rel in ALLOWED if rel in reads]
        assert len(found) >= 12, f"the census found only {found} — wrong root or broken pattern"

    def test_username_is_read_only_where_named(self):
        reads = _reads()
        stray = {rel: lines for rel, lines in reads.items() if rel not in ALLOWED}
        assert stray == {}, (
            "username read outside the named places — a person's name goes through "
            f"users.public_name.public_person_name: {stray}"
        )
        for rel in FIXED:
            assert rel not in reads, rel
        for rel, reason in ALLOWED.items():
            assert reason.strip(), rel


# --- behaviour -------------------------------------------------------------------

FORMS = [
    pytest.param("bot:max:77019140", "77019140", id="bot-proxy"),
    pytest.param("user_79991914001", "79991914001", id="phone-registered"),
    pytest.param("social_vk_19140555", "19140555", id="social"),
    pytest.param("anon_1914ab", "1914ab", id="anonymous-session"),
]


@pytest.fixture
def specialist(db):
    user = User.objects.create_user(
        username="rev1914_spec", password="x", role="specialist",
        phone="+79991914100", first_name="Зарина",
    )
    profile = user.specialist_profile
    profile.status = "active"
    profile.is_available = True
    profile.save()
    category = ServiceCategory.objects.create(name="Ногти 1914")
    service = Service.objects.create(
        specialist=profile, category=category, name="Маникюр",
        price=Decimal("1500"), duration_minutes=60,
    )
    return profile, service


def _review(specialist, *, username, phone, first_name="", last_name="", anonymous=False) -> Review:
    profile, service = specialist
    client = User.objects.create_user(
        username=username, password="x", role="client", phone=phone,
        first_name=first_name, last_name=last_name,
    )
    now = timezone.now()
    appointment = Appointment.objects.create(
        client=client, specialist=profile, service=service,
        start_datetime=now - timezone.timedelta(hours=2),
        end_datetime=now - timezone.timedelta(hours=1),
        status="completed", price=service.price,
    )
    return Review.objects.create(
        appointment=appointment, client=client, specialist=profile,
        service=service, rating=5, is_anonymous=anonymous,
    )


def _public_list(profile):
    api = APIClient()
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    resp = api.get(f"/api/v1/specialists/{profile.pk}/reviews/")
    assert resp.status_code == 200, resp.content
    return resp


class TestPublicReviews:
    @pytest.mark.parametrize("username,secret", FORMS)
    def test_a_nameless_client_is_not_published_by_username(self, specialist, username, secret):
        _review(specialist, username=username, phone="+79991914201")
        resp = _public_list(specialist[0])
        assert resp.data["data"][0]["client_name"] == CLIENT_LABEL
        body = resp.content.decode("utf-8")
        assert username not in body and secret not in body

    def test_a_deleted_client_is_not_published_by_username(self, specialist):
        review = _review(specialist, username="rev1914_gone", phone="+79991914202")
        User.objects.filter(pk=review.client_id).update(username=f"deleted:{review.client_id}")
        resp = _public_list(specialist[0])
        assert resp.data["data"][0]["client_name"] == CLIENT_LABEL
        assert "deleted:" not in resp.content.decode("utf-8")

    def test_a_named_client_keeps_the_name(self, specialist):
        _review(specialist, username="user_79991914203", phone="+79991914203",
                first_name="Анна", last_name="Петрова")
        assert _public_list(specialist[0]).data["data"][0]["client_name"] == "Анна Петрова"

    def test_an_anonymous_review_has_no_name(self, specialist):
        _review(specialist, username="user_79991914204", phone="+79991914204",
                first_name="Анна", anonymous=True)
        assert _public_list(specialist[0]).data["data"][0]["client_name"] is None


class TestReviewResponse:
    @pytest.mark.parametrize("username,secret", FORMS)
    def test_the_author_response_carries_no_username(self, specialist, username, secret):
        review = _review(specialist, username=username, phone="+79991914301")
        data = ReviewDetailSerializer(review).data
        assert data["client_name"] == CLIENT_LABEL
        assert secret not in repr(data)


class TestMasterDisplayName:
    def test_a_master_registered_by_phone_is_not_named_by_it(self, db):
        user = User.objects.create_user(
            username="user_79991914401", password="x", role="specialist", phone="+79991914401",
        )
        name = user.specialist_profile.display_name
        assert name == MASTER_LABEL
        assert "79991914401" not in name

    def test_a_named_master_keeps_the_name(self, db):
        user = User.objects.create_user(
            username="user_79991914402", password="x", role="specialist",
            phone="+79991914402", first_name="Зарина", last_name="Алиева",
        )
        assert user.specialist_profile.display_name == "Зарина Алиева"
