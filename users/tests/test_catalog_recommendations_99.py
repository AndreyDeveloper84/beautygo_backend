"""Tests for POST /api/v1/internal/me/catalog/recommendations/ (#99).

W1 booking flow Phase B endpoint. Three-layer catalog
recommendations per Tau's §10.1.

Coverage:
- Auth boundary (Bearer + X-External-User-ID; smoke — deep tests in PR #158)
- Layer 1: customer's history tenants surfaced; non-history hidden
- Layer 2: порядок и причины приходят от резолвера, поверхность их не строит
- Layer 3: category aggregate counts — агрегат каталога, не рекомендация
- Eligibility filter: inactive/disabled specialists excluded
- Fail-closed: маппинг без `VERIFIED` (§10.1); безопасность — `NOT_APPLICABLE`
  типом поверхности и не управляема вызывающим (§72)

Строк про composite score, `reasoning_text` и приоритет «goal > distance >
rating» здесь больше нет: этих механизмов не существует (T6, контракт §16).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from django.utils import timezone

from services.models import SalonService, ServiceCategory, SpecialistService
from tenants.models import Tenant
from users.catalog_recommendations_api import RecommendationsRequestSerializer
from users.models import SpecialistProfile, TenantUserRelationship, User


VALID_TOKEN = "test-ayla-internal-token-catalog"
URL = "/api/v1/internal/me/catalog/recommendations/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def external_user_id():
    return "bot:catalog"


@pytest.fixture
def customer(db, external_user_id):
    return User.objects.create_user(
        username=external_user_id, password="x", role="client",
        phone="+79994000001", is_proxy=True,
    )


@pytest.fixture
def tenant_known(db):
    return Tenant.objects.create(slug="cat-known", name="Customer's Salon")


@pytest.fixture
def tenant_new(db):
    return Tenant.objects.create(slug="cat-new", name="New Salon A")


@pytest.fixture
def tenant_explore(db):
    return Tenant.objects.create(slug="cat-explore", name="Explore Salon")


@pytest.fixture
def customer_known_tur(db, customer, tenant_known):
    """Customer has an active CUSTOMER-role TUR in tenant_known."""
    TenantUserRelationship.objects.filter(user=customer).delete()
    return TenantUserRelationship.objects.create(
        user=customer, tenant=tenant_known,
        role=TenantUserRelationship.Role.CUSTOMER,
    )


def _make_specialist(
    tenant, *, suffix: str, name: str, lat=None, lon=None,
    rating: Decimal = Decimal("4.5"), reviews: int = 10,
    is_available: bool = True, is_booking_enabled: bool = True,
    status_value: str = "active",
):
    user = User.objects.create_user(
        username=f"cat_spec_{suffix}", password="x", role="specialist",
        phone=f"+79994{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.tenant = tenant
    profile.status = status_value
    profile.is_available = is_available
    profile.is_booking_enabled = is_booking_enabled
    profile.timezone = "Europe/Moscow"
    profile.rating = rating
    profile.reviews_count = reviews
    if lat is not None:
        profile.location_lat = Decimal(str(lat))
    if lon is not None:
        profile.location_lng = Decimal(str(lon))
    profile.save()
    return profile


@pytest.fixture
def manicure_category(db):
    return ServiceCategory.objects.create(
        name="Маникюр", slug="manicure",
    )


@pytest.fixture
def massage_category(db):
    return ServiceCategory.objects.create(
        name="Массаж", slug="massage",
    )


def _make_service(
    specialist, category, *, name="Service", price="1500.00",
    mapping_status=SalonService.MappingStatus.VERIFIED, tenant=None,
):
    """Услуга КАНОНИЧЕСКИМ слоем, со статусом связи (§76).

    Здесь создавалась легаси-строка `Service`, а непустую полку тесты
    получали настройкой `RECOMMENDATION_PILOT_MAPPING_OVERRIDE=True`.
    Настройки больше нет: владелец запретил пропускать неподтверждённые
    связи, и путь убран, а не выключен (T16).

    Значит фикстура обязана давать полке то, что полка теперь требует, —
    **подтверждённую связь**. Легаси-строка её иметь не может по
    устройству слоя, поэтому и слой здесь канонический: `SalonService`
    со статусом плюс бронируемый `SpecialistService`.

    Это заодно приближает фикстуру к пилоту, где легаси пуст целиком
    (0 строк, замер 30.08). Тесты, зеленевшие на слое, которого в бою
    нет, доказывали меньше, чем казалось.

    `mapping_status` — аргумент, а не константа: тест про отказ обязан
    уметь назвать `REVIEW_REQUIRED`, не трогая настройки, которых нет.

    `tenant` — тоже аргумент, и по неочевидной причине. У салонной услуги
    тенант обязателен по схеме, а у `SpecialistProfile` он **nullable**:
    мастер без салона существует, и ровно он однажды ронял весь эндпоинт
    в 500. Такому мастеру услугу всё равно надо чем-то дать — иначе тест
    про него не собрать, — поэтому салон услуги называется отдельно
    от салона мастера. Умолчание берёт салон мастера, как и раньше.
    """
    salon = SalonService.objects.create(
        tenant=tenant or specialist.tenant,
        category=category,
        name=name,
        duration_minutes=60,
        mapping_status=mapping_status,
        **_provenance_for(mapping_status),
    )
    return SpecialistService.objects.create(
        salon_service=salon,
        specialist=specialist,
        price=Decimal(price),
        duration_minutes=60,
    )


def _names_of(rows) -> set[str]:
    """Имена по ссылкам на кандидатов.

    Строка полки несёт `candidate: {kind, id}` и **не несёт имени**:
    показ берётся из зеркала, а не отсюда (T18). Тесты продолжают
    читаться именами — так видно, про кого они, — но имя добывается
    по ключу, как это делает и настоящий потребитель.
    """
    ids = [row["candidate"]["id"] for row in rows]
    return set(
        SpecialistProfile.objects
        .filter(id__in=ids)
        .values_list("display_name", flat=True)
    )


def _provenance_for(mapping_status) -> dict:
    """`VERIFIED` без provenance не сохранится — это запрещает схема.

    Фикстура называет себя правилом честно: `test_fixture` с версией.
    Подставлять сюда человека было бы хуже — тест утверждал бы, что
    связь подтвердил кто-то, кого не существует.
    """
    if mapping_status != SalonService.MappingStatus.VERIFIED:
        return {}
    return {
        "mapping_confirmed_rule": "test_fixture",
        "mapping_rule_version": "1.0.0",
        "mapping_confirmed_at": timezone.now(),
        "mapping_source_ref": "fixture:test_catalog_recommendations_99",
    }


def _api(
    *, bearer: str | None = VALID_TOKEN,
    external_user_id: str = "bot:catalog",
) -> APIClient:
    c = APIClient()
    # /internal/me/ excluded from AppType + Tenant middleware.
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _body(**overrides) -> dict:
    """Тело запроса после T6.

    Состояния безопасности здесь НЕТ и прислать его нельзя: поверхность
    объявляет `NOT_APPLICABLE` своим типом (решение владельца §72), а поле
    в запросе завело бы запрещённую конструкцию «поля нет → неприменимо».
    Тест не подставляет того, чего ручка не принимает.
    """
    return dict(overrides)


# ---------------------------------------------------------------------------
# Auth boundary (smoke only — deep coverage in PR #158)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthBoundary:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_bearer_denied(self, customer):
        r = _api(bearer=None).post(URL, _body(), format="json")
        assert r.status_code == 403

    def test_wrong_bearer_denied(self, customer):
        r = _api(bearer="wrong").post(URL, _body(), format="json")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Layer 1 — customer's history tenants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLayer1YourPlaces:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_history_tenant_specialists_in_layer_1(
        self, customer, customer_known_tur, tenant_known,
        manicure_category,
    ):
        spec = _make_specialist(
            tenant_known, suffix="0001", name="Известный",
        )
        _make_service(spec, manicure_category, name="Маникюр")

        r = _api().post(URL, _body(), format="json")
        assert r.status_code == 200
        body = r.json()["data"]
        l1_ids = {item["candidate"]["id"] for item in body["layer_1_your_places"]}
        assert str(spec.id) in l1_ids

    def test_non_history_tenant_not_in_layer_1(
        self, customer, customer_known_tur, tenant_new,
        manicure_category,
    ):
        spec = _make_specialist(
            tenant_new, suffix="0002", name="Новый",
        )
        _make_service(spec, manicure_category, name="Маникюр")

        r = _api().post(URL, _body(), format="json")
        body = r.json()["data"]
        l1_ids = {item["candidate"]["id"] for item in body["layer_1_your_places"]}
        assert str(spec.id) not in l1_ids

    def test_no_history_returns_empty_layer_1(
        self, customer, tenant_new, manicure_category,
    ):
        """Customer has zero CUSTOMER-role TURs → layer_1 is []."""
        _make_specialist(tenant_new, suffix="0003", name="X")

        r = _api().post(URL, _body(), format="json")
        body = r.json()["data"]
        assert body["layer_1_your_places"] == []

    def test_layer_1_not_filtered_by_goal(
        self, customer, customer_known_tur, tenant_known,
        manicure_category, massage_category,
    ):
        """Code Reviewer MUST_FIX (ae4a9f66195355a6c): typing a goal
        must NOT hide the customer's known salon when that salon
        doesn't happen to offer the goal. Layer 1 is identity/
        relationship-anchored, not goal-scoped."""
        masseur = _make_specialist(
            tenant_known, suffix="0004", name="OnlyMassage",
        )
        _make_service(masseur, massage_category, name="Массаж")

        r = _api().post(URL, _body(goal="маникюр"), format="json")
        body = r.json()["data"]
        l1_ids = {item["candidate"]["id"] for item in body["layer_1_your_places"]}
        # Salon offers no manicure, but it's still in 'your places'.
        assert str(masseur.id) in l1_ids
        # Layer 2 should NOT surface this masseur — they don't match
        # the goal and aren't in history.
        l2_ids = {item["candidate"]["id"] for item in body["layer_2_ayla_picks"]}
        assert str(masseur.id) not in l2_ids

    def test_layer_1_is_not_ordered_by_rating(
        self, customer, customer_known_tur, tenant_known, manicure_category,
    ):
        """Полка «твои салоны» больше не сортируется по рейтингу.

        Было `order_by("-rating", "id")[:5]` — качество как порядок плюс
        лексикографика под отсечением, то есть мастер с «неудачным» id
        при равенстве не показывался никому. Теперь порядок даёт резолвер,
        а рейтинг в сортировку не входит вовсе (решение владельца §29.4):
        оба кандидата неразличимы и делят ярус.
        """
        for suffix, name, rating in (
            ("0300", "Top", "5.0"), ("0301", "Mid", "4.0"), ("0302", "Low", "3.0"),
        ):
            sp = _make_specialist(
                tenant_known, suffix=suffix, name=name, rating=Decimal(rating),
            )
            _make_service(sp, manicure_category)

        rows = _api().post(URL, _body(), format="json").json()["data"]["layer_1_your_places"]

        assert _names_of(rows) == {"Top", "Mid", "Low"}
        assert {row["tier"] for row in rows} == {1}


@pytest.mark.django_db
class TestLayer2AylaPicks:
    """Полка 2 после T6: проекция решения резолвера, а не своя формула."""

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_layer_2_capped_at_3(self, customer, tenant_new, manicure_category):
        """Срез — представление, а не политика.

        Резолвер отдаёт всё допустимое множество; поверхность показывает
        первые k и делает это ПОСЛЕ ротации. Иначе «Показать ещё»
        показывать нечего (§12.2).
        """
        for i in range(5):
            sp = _make_specialist(
                tenant_new, suffix=f"010{i}", name=f"Pick {i}", rating=Decimal("4.0"),
            )
            _make_service(sp, manicure_category)

        rows = _api().post(URL, _body(), format="json").json()["data"]["layer_2_ayla_picks"]
        assert len(rows) == 3

    def test_layer_2_excludes_history_tenants(
        self, customer, customer_known_tur, tenant_known, tenant_new, manicure_category,
    ):
        known = _make_specialist(tenant_known, suffix="0110", name="Known")
        fresh = _make_specialist(tenant_new, suffix="0111", name="Fresh")
        _make_service(known, manicure_category)
        _make_service(fresh, manicure_category)

        rows = _api().post(URL, _body(), format="json").json()["data"]["layer_2_ayla_picks"]
        assert _names_of(rows) == {"Fresh"}

    def test_rating_does_not_order_the_shelf(
        self, customer, tenant_new, manicure_category,
    ):
        """Решение владельца §29.4: рейтинг в сортировку НЕ входит.

        Раньше этот класс проверял обратное — «выше рейтинг, выше место».
        Формула была `rating*10 + ...`, то есть сортировкой по рейтингу
        и ничем больше. Теперь оба кандидата неразличимы и делят ярус:
        стадия качества молчит, пока свидетельство не подтверждено.
        """
        high = _make_specialist(
            tenant_new, suffix="0120", name="High", rating=Decimal("5.0"),
        )
        low = _make_specialist(
            tenant_new, suffix="0121", name="Low", rating=Decimal("3.0"),
        )
        _make_service(high, manicure_category)
        _make_service(low, manicure_category)

        rows = _api().post(URL, _body(), format="json").json()["data"]["layer_2_ayla_picks"]
        assert {row["tier"] for row in rows} == {1}

    def test_each_item_carries_codes_instead_of_a_sentence(
        self, customer, tenant_new, manicure_category,
    ):
        """WHY — коды и свидетельства, а не строка от источника (§7).

        Строку собирает представление. Источник, собравший её сам,
        однажды напечатал человеку «Рейтинг 4.9» при нуле отзывов.
        """
        sp = _make_specialist(tenant_new, suffix="0130", name="Pick", rating=Decimal("4.9"))
        _make_service(sp, manicure_category)

        rows = _api().post(URL, _body(), format="json").json()["data"]["layer_2_ayla_picks"]
        assert rows
        for row in rows:
            assert "reasoning_text" not in row
            assert row["reason_codes"]

    def test_row_declares_the_candidate_kind_and_carries_no_display_fields(
        self, customer, tenant_new, manicure_category,
    ):
        """Строка полки — ссылка на кандидата, а не карточка (T18).

        Две половины одной проверки, и ни одну нельзя опустить.

        **Вид объявлен.** Пока строка отдавала голый `id`, существовал
        ответ, который источник считал валидным, а потребитель на другой
        стороне границы молча отбрасывал целиком: он отбирал кандидатов
        вида `SERVICE`, мы производим `PROVIDER`. Совпасть это не могло
        никогда, но проявилось бы не сразу — сегодня выдача и так пуста,
        а в день, когда связи разметят, ждали бы загоревшуюся полку
        с готовым ложным объяснением «наверное, опять разметка».

        **Полей показа нет.** Имя, фото и рейтинг живут в зеркале, там же
        ключи потребителя и данные соседних блоков экрана. Прислав своё
        имя, мы завели бы второй источник тех же полей — то же
        расхождение, что убирает эпик, только в отображении.

        Проверка по МНОЖЕСТВУ ключей, а не по наличию нужных: поле,
        добавленное завтра «просто чтобы было», сломает этот тест
        сегодняшним запуском.
        """
        sp = _make_specialist(tenant_new, suffix="0170", name="Ссылка")
        _make_service(sp, manicure_category)

        row = _api().post(URL, _body(), format="json").json()["data"]["layer_2_ayla_picks"][0]

        assert row["candidate"] == {"kind": "PROVIDER", "id": str(sp.id)}
        assert set(row) == {"candidate", "rank", "tier", "reason_codes", "evidence"}

    def test_unsubstantiated_rating_is_delivered_but_not_a_reason(
        self, customer, tenant_new, manicure_category,
    ):
        """Число доезжает как справочное, силой объявлено недоказанным.

        Разделение «показать» и «обосновать» проводится в источнике,
        а не на поверхности — иначе каждая поверхность проведёт его
        по-своему, что уже однажды и случилось.
        """
        sp = _make_specialist(
            tenant_new, suffix="0140", name="Loud", rating=Decimal("4.9"), reviews=0,
        )
        _make_service(sp, manicure_category)

        row = _api().post(URL, _body(), format="json").json()["data"]["layer_2_ayla_picks"][0]
        ratings = [item for item in row["evidence"] if item["kind"] == "RATING"]
        assert ratings and ratings[0]["strength"] == "UNSUBSTANTIATED"
        assert not any(
            code == "QUALITY_RATING_SUBSTANTIATED" for code in row["reason_codes"]
        )


@pytest.mark.django_db
class TestFailClosedStates:
    """Состояния, которые обязаны отличаться от «подходящих нет».

    Их было два. Владелец ответил на один (§72): безопасность на этой
    поверхности не «неизвестна», а неприменима, и полку больше не гасит.
    Остался маппинг — он ждёт §40.4 п.1.
    """

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    @pytest.mark.parametrize(
        "status",
        [
            SalonService.MappingStatus.REVIEW_REQUIRED,
            SalonService.MappingStatus.UNMAPPED,
        ],
    )
    def test_only_verified_reaches_the_shelf(
        self, customer, tenant_new, manicure_category, status,
    ):
        """§10.1 + §76: `catalog_visible ≠ recommendation_eligible`.

        `REVIEW_REQUIRED` здесь — не абстракция: именно его получат все
        206 связей пилота после миграции, и именно его владелец запретил
        считать достаточным («иначе статус будет декоративным»).

        Раньше этот тест выключал настройку. Настройки больше нет —
        и проверять теперь надо не её, а сам статус: **тест, который
        проходит по причине, которой больше не существует, — ложный
        сторож.**

        Полка 3 при этом жива, и это половина смысла: каталог видно,
        рекомендовать нельзя — разные вещи, и человек не остаётся перед
        пустым экраном.
        """
        sp = _make_specialist(tenant_new, suffix="0150", name="Invisible")
        _make_service(sp, manicure_category, mapping_status=status)

        data = _api().post(URL, _body(), format="json").json()["data"]
        assert data["layer_2_ayla_picks"] == []
        assert data["layer_3_explore"]["categories"]

    def test_safety_is_declared_by_the_surface_and_not_steerable_by_the_caller(
        self, customer, tenant_new, manicure_category, settings,
    ):
        """§72: состояние безопасности здесь — свойство ручки, не поле запроса.

        Один тест закрывает оба ограничения владельца сразу.

        **Гейт не применяется.** Раньше отсутствие поля означало `UNKNOWN`
        и гасило полку. Пустая полка ничего не предотвращала: те же мастера
        видны и бронируемы в обычном каталоге одним тапом — закрытым
        оказывалось объяснение, а не действие. Теперь полка живая, и это
        обязано быть видно тестом: иначе правка неотличима от кода, который
        просто гасит `NOT_APPLICABLE` всегда, то есть от возврата к
        fail-closed.

        **Прислать состояние нельзя.** `STOP` в теле не меняет ничего —
        не потому, что его аккуратно отбрасывают, а потому, что пути
        от данных запроса к гейту не существует: поля нет в схеме,
        значение — константа поверхности. Ровно этим отличается «тип
        поверхности до решения» от запрещённого «данных нет → неприменимо»:
        второе управляемо тем, кто зовёт, первое — нет.

        Отмену заявления содержанием решения (кандидат с
        `requires_health_check`, активная S4) стережёт резолвер, там же,
        где она и живёт: `recommendation/tests/test_safety_not_applicable.py`.
        Дублировать её здесь значило бы проверять чужую стадию через ручку.
        """
        sp = _make_specialist(tenant_new, suffix="0160", name="Visible")
        _make_service(sp, manicure_category)

        without = _api().post(URL, _body(), format="json").json()["data"]
        steered = _api().post(
            URL, _body(safety_state="STOP"), format="json",
        ).json()["data"]

        assert [row["candidate"]["id"] for row in without["layer_2_ayla_picks"]] == [str(sp.id)]
        assert steered["layer_2_ayla_picks"] == without["layer_2_ayla_picks"]

    def test_request_schema_carries_no_safety_field(self):
        """Сторож на схему: поля нет — значит и умолчания у него нет.

        Сторож по исходникам (`test_safety_not_applicable.py`) ловит
        `x or NOT_APPLICABLE` и `default=NOT_APPLICABLE`. Он НЕ ловит
        поле, объявленное `required=False` без умолчания: такое поле
        само по себе невинно, а дыру открывает вместе со строкой
        в обработчике. Здесь стережётся вторая половина — само наличие
        входа для состояния безопасности на этой поверхности.
        """
        fields = RecommendationsRequestSerializer().get_fields()
        assert not [name for name in fields if "safety" in name]


@pytest.mark.django_db
class TestEligibilityFilter:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_unavailable_specialist_excluded(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0200", name="Unavailable",
            is_available=False,
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, _body(), format="json")
        body = r.json()["data"]
        all_ids = (
            {it["candidate"]["id"] for it in body["layer_1_your_places"]}
            | {it["candidate"]["id"] for it in body["layer_2_ayla_picks"]}
        )
        assert str(sp.id) not in all_ids

    def test_booking_disabled_specialist_excluded(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0201", name="NoBookings",
            is_booking_enabled=False,
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, _body(), format="json")
        body = r.json()["data"]
        all_ids = (
            {it["candidate"]["id"] for it in body["layer_1_your_places"]}
            | {it["candidate"]["id"] for it in body["layer_2_ayla_picks"]}
        )
        assert str(sp.id) not in all_ids

    def test_non_active_status_excluded(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0202", name="Pending",
            status_value="pending",
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, _body(), format="json")
        body = r.json()["data"]
        all_ids = (
            {it["candidate"]["id"] for it in body["layer_1_your_places"]}
            | {it["candidate"]["id"] for it in body["layer_2_ayla_picks"]}
        )
        assert str(sp.id) not in all_ids


# ---------------------------------------------------------------------------
# Goal filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGoalFilter:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_goal_filters_by_service_name(
        self, customer, tenant_new, manicure_category, massage_category,
    ):
        manicurist = _make_specialist(
            tenant_new, suffix="0300", name="Manicurist",
        )
        masseur = _make_specialist(
            tenant_new, suffix="0301", name="Masseur",
        )
        _make_service(manicurist, manicure_category, name="Маникюр")
        _make_service(masseur, massage_category, name="Массаж")

        r = _api().post(URL, _body(goal="маникюр"), format="json")
        body = r.json()["data"]
        l2_ids = {it["candidate"]["id"] for it in body["layer_2_ayla_picks"]}
        assert str(manicurist.id) in l2_ids
        assert str(masseur.id) not in l2_ids

    def test_goal_filters_by_category_slug(
        self, customer, tenant_new, manicure_category, massage_category,
    ):
        manicurist = _make_specialist(
            tenant_new, suffix="0310", name="Manicurist2",
        )
        masseur = _make_specialist(
            tenant_new, suffix="0311", name="Masseur2",
        )
        _make_service(
            manicurist, manicure_category, name="Шеллак",
        )
        _make_service(masseur, massage_category, name="Шиацу")

        r = _api().post(URL, _body(goal="manicure"), format="json")
        body = r.json()["data"]
        l2_ids = {it["candidate"]["id"] for it in body["layer_2_ayla_picks"]}
        assert str(manicurist.id) in l2_ids
        assert str(masseur.id) not in l2_ids


# ---------------------------------------------------------------------------
# Distance + reasoning text
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGeographyIsNotOnThisSurface:
    """География ушла со шкалы карточки — и это не потеря.

    Прежние тесты этого класса проверяли `distance_km` в карточке и
    фразу «1.2 км от вас» в обосновании. Оба поля мертвы по факту:
    фронт шлёт пустое тело, `lat`/`lon` не приходили НИКОГДА, значит
    расстояние всегда было `None`, а слагаемое `100/(km+1)` не
    срабатывало ни разу.

    По контракту §3.3 расстояние допускается только как жёсткий предел
    радиуса в S0/S1 и никогда как слагаемое. Радиус — поле запроса
    резолвера; он появится здесь вместе с настоящей географией, а не
    вместе с полем, которое всегда пусто.
    """

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_card_has_no_distance_field(self, customer, tenant_new, manicure_category):
        sp = _make_specialist(tenant_new, suffix="0200", name="Near")
        _make_service(sp, manicure_category)

        row = _api().post(URL, _body(), format="json").json()["data"]["layer_2_ayla_picks"][0]
        assert "distance_km" not in row


@pytest.mark.django_db
class TestLayer3Explore:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_layer_3_returns_category_counts(
        self, customer, tenant_new, manicure_category, massage_category,
    ):
        m1 = _make_specialist(
            tenant_new, suffix="0500", name="M1",
        )
        m2 = _make_specialist(
            tenant_new, suffix="0501", name="M2",
        )
        ms1 = _make_specialist(
            tenant_new, suffix="0502", name="MS1",
        )
        _make_service(m1, manicure_category)
        _make_service(m2, manicure_category)
        _make_service(ms1, massage_category)

        r = _api().post(URL, _body(), format="json")
        cats = r.json()["data"]["layer_3_explore"]["categories"]
        by_slug = {c["slug"]: c for c in cats}
        assert "manicure" in by_slug
        assert "massage" in by_slug
        # Manicure has 2 services, massage has 1.
        assert by_slug["manicure"]["count"] == 2
        assert by_slug["massage"]["count"] == 1


# ---------------------------------------------------------------------------
# Состояние салона решает пул — DRF-1430
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSalonStateGatesThePool:
    """Пул обязан исполнять то, что обещает его докстринг.

    Докстринг говорил «active specialist in an active tenant taking
    bookings», а фильтра по салону в коде не было вовсе: `select_related`
    по салону служил только выводу имени салона в карточке. Отключённый
    салон попадал в «ваши места» наравне с живыми.

    Карточки с тех пор не стало (T18 — полка несёт ссылку на кандидата,
    показ берётся из зеркала), но проверка осталась и осталась нужной:
    фильтр по состоянию салона — про допуск, а не про отрисовку, и от
    смены формы ответа он не зависит.

    Правило контура: рядом с каждым отрицательным утверждением стоит
    положительная стража НА ТЕХ ЖЕ ДАННЫХ — иначе «мастера не видно»
    зелено и на пустом ответе.
    """

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    @staticmethod
    def _layer_1_ids() -> set[str]:
        r = _api().post(URL, _body(), format="json")
        assert r.status_code == 200, r.content
        return {i["candidate"]["id"] for i in r.json()["data"]["layer_1_your_places"]}

    @staticmethod
    def _layer_2_ids() -> set[str]:
        r = _api().post(URL, _body(), format="json")
        assert r.status_code == 200, r.content
        return {i["candidate"]["id"] for i in r.json()["data"]["layer_2_ayla_picks"]}

    def test_deactivated_salon_drops_out_of_your_places(
        self, customer, customer_known_tur, tenant_known, manicure_category,
    ):
        spec = _make_specialist(
            tenant_known, suffix="0601", name="Мастер знакомого салона",
        )
        _make_service(spec, manicure_category, name="Маникюр")

        # Положительная стража: салон включён — мастер на месте.
        # Без неё отрицание ниже прошло бы и на пустом ответе.
        assert str(spec.id) in self._layer_1_ids()

        # Меняем РОВНО одно поле — состояние салона.
        tenant_known.is_active = False
        tenant_known.save(update_fields=["is_active"])
        assert str(spec.id) not in self._layer_1_ids()

        # И обратно, чтобы исключить любую другую причину.
        tenant_known.is_active = True
        tenant_known.save(update_fields=["is_active"])
        assert str(spec.id) in self._layer_1_ids()

    @pytest.mark.no_auto_tenant
    def test_master_without_a_salon_does_not_500_the_endpoint(
        self, customer, tenant_new, manicure_category,
    ):
        """Мастер без салона больше не роняет ВЕСЬ эндпоинт в 500.

        ``_build_card`` разыменовывает ``specialist.tenant.slug`` и
        ``.name`` без проверки на ``None``, а ``SpecialistProfile.tenant``
        — ``null=True`` (бэкфилл DRF-242.4 не закрыт). До DRF-1430 такой
        профиль попадал в пул и клал ответ целиком:

            AttributeError: 'NoneType' object has no attribute 'slug'

        То есть страдал не только он сам — 500 получал каждый клиент,
        чей пул его зацепил. INNER JOIN в ``_base_pool`` превращает
        жёсткое падение в корректное отсутствие.

        Это НЕ то же решение, что в ``RecommendationEngine``: там
        мастера без салона остаются в выдаче, потому что движок тенант
        не разыменовывает и отдать такого мастера может.
        """
        orphan = _make_specialist(
            None, suffix="0602", name="Мастер без салона",
        )
        # Салон услуги называем явно: у мастера его нет, а у услуги он
        # обязателен по схеме. Предмет теста — мастер без салона,
        # и он таким и остаётся.
        _make_service(orphan, manicure_category, name="Маникюр", tenant=tenant_new)

        # Стража на предусловие: профиль действительно без салона.
        orphan.refresh_from_db()
        assert orphan.tenant_id is None

        # Положительная стража НА ТЕХ ЖЕ данных: мастер с живым салоном
        # рядом — чтобы «200 и без сироты» не доказывалось пустым
        # ответом, в котором нет вообще никого.
        healthy = _make_specialist(
            tenant_new, suffix="0603", name="Мастер живого салона",
        )
        _make_service(healthy, manicure_category, name="Маникюр")

        r = _api().post(URL, _body(), format="json")
        assert r.status_code == 200, r.content

        picks = {i["candidate"]["id"] for i in r.json()["data"]["layer_2_ayla_picks"]}
        assert str(healthy.id) in picks
        assert str(orphan.id) not in picks
