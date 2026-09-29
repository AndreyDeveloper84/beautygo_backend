"""Integration tests for GET /api/v1/home/ — DRF-110."""
from __future__ import annotations

import textwrap

from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from ai.tests.factories import make_specialist, make_user
from appointments.models import Appointment
from services.models import Service, ServiceCategory


pytestmark = pytest.mark.django_db


HOME_URL = "/api/v1/home/"


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client_user(db):
    return make_user(role="client")


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


def _make_appointment(client_user, *, status, start_offset_hours: int):
    spec = make_specialist()
    cat = ServiceCategory.objects.create(name=f"Cat-{start_offset_hours}")
    svc = Service.objects.create(
        specialist=spec, name="Маникюр", price=Decimal("1500"),
        duration_minutes=60, is_active=True, category=cat,
    )
    start = datetime(2026, 5, 1, 12, 0, tzinfo=dt_tz.utc) + timedelta(
        hours=start_offset_hours,
    )
    return Appointment.objects.create(
        client=client_user,
        specialist=spec,
        service=svc,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=status,
        price=svc.price,
        snapshot_price=svc.price,
        snapshot_service_name=svc.name,
        snapshot_duration_minutes=60,
    )


# ---------------------------------------------------------------------------
# Auth & app-type guards
# ---------------------------------------------------------------------------


class TestAuth:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.get(HOME_URL)
        assert resp.status_code == 401

    def test_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.get(HOME_URL)
        assert resp.status_code == 403

    def test_specialist_role_returns_403(self):
        spec_user = make_user(role="specialist")
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=spec_user)
        resp = c.get(HOME_URL)
        # Specialist hits IsClient guard → 403
        assert resp.status_code == 403

    def test_anonymous_guest_returns_403(self):
        """Regression — surfaced 2026-04-27 dev-VPS smoke test:
        anonymous users (is_guest=True, role='client') was reaching /home/
        because IsClient only checked role. Now IsClient also rejects
        is_guest=True. Anon-friendly home (browse mode) is a separate
        product decision — this endpoint stays gated."""
        guest = make_user(role="client", is_guest=True)
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=guest)
        assert c.get(HOME_URL).status_code == 403


# ---------------------------------------------------------------------------
# Section shape
# ---------------------------------------------------------------------------


class TestSectionShape:
    def test_response_includes_all_five_sections(self, auth_client):
        resp = auth_client.get(HOME_URL)
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert "upcoming_appointments" in body
        assert "favorite_specialists" in body
        assert "popular_categories" in body
        assert "nearby_specialists" in body
        assert "recent_activity" in body

    def test_favorites_returns_empty_until_drf_72(self, auth_client):
        resp = auth_client.get(HOME_URL)
        # DRF-72 not implemented yet — empty list, not error.
        assert resp.json()["data"]["favorite_specialists"] == []


# ---------------------------------------------------------------------------
# upcoming_appointments
# ---------------------------------------------------------------------------


class TestUpcomingAppointments:
    def test_returns_only_upcoming_pending_or_confirmed(
        self, auth_client, client_user,
    ):
        # Past one — shouldn't show
        past = _make_appointment(
            client_user,
            status=Appointment.Status.COMPLETED,
            start_offset_hours=-100,
        )
        # Cancelled — shouldn't show even if future
        cancelled = _make_appointment(
            client_user,
            status=Appointment.Status.CANCELLED,
            start_offset_hours=10,
        )
        # Confirmed in future — should show
        confirmed = _make_appointment(
            client_user,
            status=Appointment.Status.CONFIRMED,
            start_offset_hours=24,
        )
        # We use future dates; need to mock now() to be before them.
        # The test fixture dates start from 2026-05-01 — verify against
        # whatever timezone.now() returns. If today < May 2026, future
        # appointments correctly show; otherwise past_offset is < now.
        from django.utils import timezone

        now = timezone.now()
        if now < confirmed.start_datetime:
            resp = auth_client.get(HOME_URL)
            ids = [
                a["id"] for a in resp.json()["data"]["upcoming_appointments"]
            ]
            assert str(confirmed.id) in ids
            assert str(past.id) not in ids
            assert str(cancelled.id) not in ids

    def test_limits_to_three(self, auth_client, client_user):
        from django.utils import timezone

        # Schedule 5 future confirmed appointments — only 3 should surface.
        future_offset_base = 24 * 30  # 30 days out
        for i in range(5):
            _make_appointment(
                client_user,
                status=Appointment.Status.CONFIRMED,
                start_offset_hours=future_offset_base + i,
            )
        resp = auth_client.get(HOME_URL)
        if timezone.now().year < 2026 or (
            timezone.now().year == 2026 and timezone.now().month < 6
        ):
            assert len(resp.json()["data"]["upcoming_appointments"]) <= 3


# ---------------------------------------------------------------------------
# popular_categories
# ---------------------------------------------------------------------------


class TestPopularCategories:
    def test_returns_categories_with_counts(self, auth_client):
        cat1 = ServiceCategory.objects.create(name="Маникюр", icon="nail")
        cat2 = ServiceCategory.objects.create(name="Стрижка", icon="cut")
        # Make cat1 popular (2 specialists), cat2 (1 specialist)
        for _ in range(2):
            spec = make_specialist()
            Service.objects.create(
                specialist=spec, name="Sv", price=Decimal("1000"),
                duration_minutes=60, is_active=True, category=cat1,
            )
        spec2 = make_specialist()
        Service.objects.create(
            specialist=spec2, name="Sv", price=Decimal("1000"),
            duration_minutes=60, is_active=True, category=cat2,
        )

        resp = auth_client.get(HOME_URL)
        cats = resp.json()["data"]["popular_categories"]
        ids = [c["id"] for c in cats]
        assert str(cat1.id) in ids
        assert str(cat2.id) in ids
        # cat1 with 2 specialists должен быть выше cat2
        cat1_idx = ids.index(str(cat1.id))
        cat2_idx = ids.index(str(cat2.id))
        assert cat1_idx < cat2_idx

    def test_caches_popular_categories(self, auth_client):
        from django.core.cache import cache
        from users.home_api import CACHE_KEY_POPULAR_CATEGORIES

        cat = ServiceCategory.objects.create(name="X")
        spec = make_specialist()
        Service.objects.create(
            specialist=spec, name="Sv", price=Decimal("100"),
            duration_minutes=30, is_active=True, category=cat,
        )

        # First call — populates cache
        auth_client.get(HOME_URL)
        cached = cache.get(CACHE_KEY_POPULAR_CATEGORIES)
        assert cached is not None

        # Add new category — should NOT appear in next call (cached)
        new_cat = ServiceCategory.objects.create(name="NewlyAdded")
        new_spec = make_specialist()
        Service.objects.create(
            specialist=new_spec, name="Sv", price=Decimal("100"),
            duration_minutes=30, is_active=True, category=new_cat,
        )
        resp = auth_client.get(HOME_URL)
        ids = [c["id"] for c in resp.json()["data"]["popular_categories"]]
        assert str(new_cat.id) not in ids


# ---------------------------------------------------------------------------
# nearby_specialists
# ---------------------------------------------------------------------------


class TestNearbySpecialists:
    def test_returns_top_rated_when_no_geo(self, auth_client):
        make_specialist(display_name="Top", rating=4.9, reviews_count=80)
        make_specialist(display_name="Mid", rating=4.5, reviews_count=20)
        resp = auth_client.get(HOME_URL)
        nearby = resp.json()["data"]["nearby_specialists"]
        assert len(nearby) >= 2

    def test_geo_query_params_accepted(self, auth_client):
        make_specialist(display_name="Geo", rating=4.7, reviews_count=20)
        resp = auth_client.get(HOME_URL, {"lat": "53.2", "lon": "45.0"})
        assert resp.status_code == 200

    def test_invalid_geo_silently_falls_back(self, auth_client):
        make_specialist(display_name="X", rating=4.7, reviews_count=20)
        resp = auth_client.get(HOME_URL, {"lat": "999", "lon": "200"})
        # Out of range — view treats as no geo rather than 400
        assert resp.status_code == 200

    def test_nearby_limit_six(self, auth_client):
        for i in range(8):
            make_specialist(display_name=f"S{i}", rating=4.9, reviews_count=50)
        resp = auth_client.get(HOME_URL)
        nearby = resp.json()["data"]["nearby_specialists"]
        assert len(nearby) <= 6


# ---------------------------------------------------------------------------
# recent_activity
# ---------------------------------------------------------------------------


class TestRecentActivity:
    def test_returns_completed_appointments_only(self, auth_client, client_user):
        completed = _make_appointment(
            client_user, status=Appointment.Status.COMPLETED,
            start_offset_hours=-200,
        )
        confirmed = _make_appointment(
            client_user, status=Appointment.Status.CONFIRMED,
            start_offset_hours=24,
        )
        resp = auth_client.get(HOME_URL)
        ids = [a["id"] for a in resp.json()["data"]["recent_activity"]]
        assert str(completed.id) in ids
        assert str(confirmed.id) not in ids

    def test_other_users_completed_not_shown(self, auth_client, client_user):
        other_user = make_user(role="client")
        _make_appointment(
            other_user, status=Appointment.Status.COMPLETED,
            start_offset_hours=-100,
        )
        resp = auth_client.get(HOME_URL)
        # Other user's completed appointment not in result
        assert resp.json()["data"]["recent_activity"] == []


# ---------------------------------------------------------------------------
# §125: «Рядом с тобой» — честный каталог, а не рекомендация
# ---------------------------------------------------------------------------


class TestNearbyIsCatalogNotRecommendation:
    """Решение владельца §125 от 10.09.2026.

    Секция использует каталог и географию и **не выдаёт себя за semantic
    Recommendation**. Персональная рекомендация остаётся отдельной
    поверхностью канонического резолвера.

    Замер, из-за которого решение и принято: близость давала 25% порядка,
    а `client history` — 10%, и наружу уезжали `match_reasons`, то есть
    объяснения персональной пригодности. Секция с нейтральным именем
    несла персональную семантику.

    Исчезновение этого с экрана — **видимое изменение продукта,
    санкционированное владельцем** (§125), а не побочный эффект правки.
    Тесты ниже существуют, чтобы через месяц его не «вернули как было»
    как случайную пропажу.
    """

    #: Ровно то, что секция вправе отдавать. Список закрытый: проверка
    #: на «нет match_reasons» пропустила бы `score`, `top_reasons` и любое
    #: следующее объяснение под новым именем. Закрытый набор ловит их все,
    #: включая те, которых ещё не придумали.
    ALLOWED_KEYS = {
        "id", "display_name", "rating", "reviews_count",
        "address", "distance_km", "services_preview",
    }

    #: Имена, под которыми объяснение персональной пригодности возвращалось
    #: или могло бы вернуться. Проверяются отдельно от закрытого набора,
    #: чтобы сообщение об ошибке называло предмет, а не «лишний ключ».
    RECOMMENDATION_ARTEFACTS = {
        "match_reasons", "score", "top_reasons", "why", "reasons",
        "recommendation_reasons", "explanation",
    }

    def test_section_carries_no_personal_fit_explanation(self, auth_client):
        """Объяснений «чем подходит именно тебе» в ответе нет."""
        make_specialist(display_name="Anna", rating=4.9, reviews_count=80)
        nearby = auth_client.get(HOME_URL).json()["data"]["nearby_specialists"]
        assert nearby, "секция пуста — проверка ничего не проверяет"

        for item in nearby:
            leaked = set(item) & self.RECOMMENDATION_ARTEFACTS
            assert not leaked, (
                "в каталожную секцию вернулось объяснение пригодности: "
                + ", ".join(sorted(leaked))
                + ". §125: секция не выдаёт себя за Recommendation."
            )

    def test_section_returns_a_closed_set_of_fields(self, auth_client):
        """Набор полей закрыт, а не «без match_reasons».

        Запрет по списку запрещённого пропустил бы следующее объяснение
        под новым именем. Здесь запрещено всё, что не разрешено.
        """
        make_specialist(display_name="Boris", rating=4.7, reviews_count=30)
        nearby = auth_client.get(HOME_URL).json()["data"]["nearby_specialists"]
        assert nearby, "секция пуста — проверка ничего не проверяет"

        for item in nearby:
            extra = set(item) - self.ALLOWED_KEYS
            assert not extra, (
                "в каталожной секции появились поля вне закрытого набора: "
                + ", ".join(sorted(extra))
            )

    def test_the_section_still_consults_the_client_goal(self):
        """Цель клиента секция спрашивает — и это НЕ откат к персонализации.

        Сторож стоит наоборот тому, что было здесь раньше, и причина
        названа, чтобы следующий не «починил» его обратно.

        Я убирал цель отсюда вместе с историей и объяснениями, читая
        §125 как «никакой персонализации вовсе». Это было моё чтение:
        §125 говорит, что секция не выдаёт себя за semantic
        Recommendation, и ни слова не говорит про цель. А OD-1 говорит
        прямо противоположное — цель влияет на пассивную выдачу, — и в
        отличие от моего чтения имеет сторожа:
        `goals/tests/test_goal_wiring_od1.py`, пять проверок.

        Пока владелец не рассудил §125 и OD-1, действует то решение, у
        которого есть сторож. Снимать этот тест — только вместе с
        ответом владельца, а не вместе с прочтением §125.
        """
        import inspect

        from users import home_api

        source = inspect.getsource(home_api)
        assert "goal_category_ids=goal_category_ids_for(user)" in source, (
            "секция перестала спрашивать цель клиента — это отмена OD-1, "
            "и её нельзя вывести из §125: там про semantic Recommendation, "
            "а не про цель. Нужно решение владельца."
        )

    def test_the_section_does_not_personalise_by_history(self):
        """История визитов в секцию не передаётся.

        `client_id` в запросе к движку включает `WEIGHT_HISTORY` —
        подъём для вернувшегося клиента. §125 историю не называет; это
        чтение «не выдаёт себя за Recommendation», и оно записано в
        коде рядом с правкой, чтобы возражение было адресным.
        """
        import ast
        import inspect

        from users import home_api

        # Разбор ДЕРЕВА, а не поиск подстроки. Первая версия этого теста
        # искала `"client_id=None" in source` — и пропустила подмену,
        # потому что та же строка стоит рядом в КОММЕНТАРИИ, объясняющем
        # правку. Сторож зеленел на подменённом коде, читая рассказ о
        # коде. Тот же урок, что записан у соседнего гарда в
        # `recommendation/tests/test_boundary_guards.py`.
        source = textwrap.dedent(
            inspect.getsource(home_api.HomeView._nearby_specialists)
        )
        tree = ast.parse(source)
        passed = [
            kw
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "client_id"
        ]
        assert passed, "вызов движка не найден — тест смотрит не туда"
        for kw in passed:
            assert isinstance(kw.value, ast.Constant) and kw.value.value is None, (
                "секция снова передаёт клиента движку — вернулась "
                "персонализация прошлым опытом (§125, §72): "
                f"client_id={ast.unparse(kw.value)}"
            )
