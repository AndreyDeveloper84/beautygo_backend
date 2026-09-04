"""Матрица распознавания названной услуги — DRF-1461 и DRF-1455.

Почему матрица, а не набор отдельных проверок
----------------------------------------------

Оба дефекта дожили до слияния по одной причине: у утверждения «стало
распознаваться лучше» не было стражи с другой стороны. Проверка,
которая умеет только подтверждать рост распознавания, не может
провалиться — а значит, ничего и не проверяет.

Поэтому здесь два списка, и они равноправны:

* ``MUST_MATCH``   — фразы, которые ОБЯЗАНЫ распознаваться;
* ``MUST_NOT_MATCH`` — фразы, которые ОБЯЗАНЫ НЕ распознаваться.

Второй список — не довесок. «нужен уход за собой» распознавалось как
услуга «Уход» ещё до DRF-1461, и морфология этот случай усиливает.
Послабление, ломающее вторую половину, хуже, чем отсутствие
послабления.

Салонная половина (DRF-1455) устроена так же: клиент салона A называет
услугу своего салона — распознаётся; называет услугу, которая есть
только у салона B, — НЕ распознаётся и получает уточнение с выходом
«Найти услугу».

Все проверки идут через ``build_decision_context`` — то есть через тот
самый путь, по которому решение принимается в бою, а не через
внутреннюю функцию, которую можно починить, не починив поведение.
"""
from __future__ import annotations

import pytest

from goals.decision_context import (
    MISSING_GOAL_CLARIFICATION,
    NEXT_BROWSE_CATALOG,
    build_decision_context,
)
from goals.models import ClientGoal
from services.models import SalonService, ServiceCategory
from tenants.models import Tenant
from users.models import TenantUserRelationship, User


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def salon_a(db):
    return Tenant.objects.create(slug="salon-a", name="Салон A")


@pytest.fixture
def salon_b(db):
    return Tenant.objects.create(slug="salon-b", name="Салон B")


@pytest.fixture
def anketa_off(settings):
    """Анкета выключена, чтобы документ отвечал про ЦЕЛЬ, а не про шаг.

    С включённой анкетой открытый проход перехватывает ``missing`` и
    вопрос про распознавание услуги остаётся без ответа. Матчинг при
    этом живёт под тем же флагом (``service_match=anketa_on``), поэтому
    отдельный класс ниже проверяет матрицу и с включённой анкетой.
    """
    settings.GOAL_ANKETA_ENABLED = True
    settings.GOAL_SERVICE_MATCH_MORPHOLOGY = True


@pytest.fixture
def client_of_a(db, salon_a):
    user = User.objects.create_user(
        username="bot:matrix-a", password="x", role="client",
        phone="+79995000101", is_proxy=True,
    )
    TenantUserRelationship.objects.get_or_create(
        user=user, tenant=salon_a, is_active=True,
        defaults={"role": TenantUserRelationship.Role.CUSTOMER},
    )
    return user


def _salon_service(tenant, name: str) -> SalonService:
    """Услуга салона. ``SalonService.clean`` требует категорию у
    внетаксономических строк — заводим служебную, её имя в матрице не
    участвует."""
    holder, _ = ServiceCategory.objects.get_or_create(
        slug=f"holder-{tenant.slug}",
        defaults={"name": f"Прочее {tenant.slug}", "tenant": tenant},
    )
    return SalonService.objects.create(
        tenant=tenant, name=name, category=holder, template=None, is_active=True,
    )


@pytest.fixture
def catalog_a(db, salon_a):
    """Каталог салона A — те же имена, что на пилоте.

    «Уход» здесь не для красоты: это ровно та строка, из-за которой
    «нужен уход за собой» распознавалось ошибочно.
    """
    ServiceCategory.objects.create(name="Маникюр", slug="m-manicure", tenant=salon_a)
    ServiceCategory.objects.create(name="Уход", slug="m-care", tenant=salon_a)
    ServiceCategory.objects.create(name="Стрижка", slug="m-haircut", tenant=salon_a)
    for name in ("Маникюр с покрытием", "Педикюр", "Массаж"):
        _salon_service(salon_a, name)


def _recognized(user, phrase: str) -> bool:
    """Признана ли фраза готовой целью — по документу состояния.

    Пусто в ``missing`` — цель готова, человек идёт к подбору.
    ``goal_clarification`` — не признана, человек получает уточнение.
    """
    ClientGoal.objects.filter(client=user).delete()
    ClientGoal.objects.create(
        client=user, goal_text=phrase, source_channel=ClientGoal.SourceChannel.BOT,
    )
    doc = build_decision_context(user)
    kinds = {item.get("kind") for item in doc["missing"]}
    assert kinds <= {MISSING_GOAL_CLARIFICATION}, kinds
    # Выход «Найти услугу» обязан быть в документе в ОБОИХ исходах —
    # нераспознанный не должен оказаться заперт на уточнении.
    assert doc["next"]["id"] == NEXT_BROWSE_CATALOG
    return not doc["missing"]


# ---------------------------------------------------------------------------
# DRF-1461 — обе стороны замера
# ---------------------------------------------------------------------------

#: ОБЯЗАНО распознаваться. Падежи, предложные конструкции, «записаться
#: на …» — то, что разрешило решение владельца 04.09.2026.
MUST_MATCH = [
    ("хочу маникюр", "именительный — работало и до правки"),
    ("хочу маникюра", "родительный — ядро замера DRF-1461"),
    ("маникюра", "голая словоформа без обрамления"),
    ("маникюр", "голое имя"),
    ("записаться на маникюр", "предложная конструкция"),
    ("Записаться на педикюр", "заглавная буква не меняет ответа"),
    ("нужен маникюр", "«нужен» — обрамление, не содержание"),
    ("мне нужен педикюр", "местоимение в обрамлении"),
    ("хочется маникюра", "безличная форма + родительный"),
    ("хочу стрижку", "винительный"),
    ("стрижка", "имя, которого не было в каталоге замера"),
    ("сделать маникюр с покрытием", "многословное имя целиком"),
    ("хочу маникюр с покрытием", "длинное имя выигрывает у короткого"),
    ("можно записаться на массаж", "вежливое обрамление"),
    ("массажа", "услуга салона в родительном"),
]

#: ОБЯЗАНО НЕ распознаваться. Ложное срабатывание из замера, общие
#: слова и обращения к боту.
MUST_NOT_MATCH = [
    ("нужен уход за собой", "ЛОЖНОЕ срабатывание из замера DRF-1461"),
    ("хочу уход за собой", "оно же в другом обрамлении"),
    ("уход за кожей", "«за кожей» — содержание, которого нет в имени"),
    ("расскажи про уход", "«расскажи», «про» — не обрамление"),
    ("удали мои данные", "обращение к боту"),
    ("сотри всё", "обращение к боту"),
    ("помощь", "обращение к боту"),
    ("отмена", "обращение к боту"),
    ("найди мастера", "обращение к боту"),
    ("хочу что-то для рук", "общие слова, услуга не названа"),
    ("не знаю, чего хочу", "прямое «не знаю»"),
    ("хочу изменить свой уход за кожей лица", "рассказ, а не имя"),
    ("массаж делает мой муж", "имя есть, но текст не про запись"),
]


@pytest.mark.django_db
class TestPhraseMatrix:
    """Замер DRF-1461 — обе стороны на одном каталоге."""

    @pytest.mark.parametrize("phrase,why", MUST_MATCH, ids=[p for p, _ in MUST_MATCH])
    def test_must_match(self, anketa_off, client_of_a, catalog_a, phrase, why):
        assert _recognized(client_of_a, phrase), f"должно распознаваться: {why}"

    @pytest.mark.parametrize(
        "phrase,why", MUST_NOT_MATCH, ids=[p for p, _ in MUST_NOT_MATCH],
    )
    def test_must_not_match(self, anketa_off, client_of_a, catalog_a, phrase, why):
        assert not _recognized(client_of_a, phrase), (
            f"НЕ должно распознаваться: {why}"
        )

    def test_morphology_off_keeps_literal_behaviour(
        self, anketa_off, settings, client_of_a, catalog_a,
    ):
        """Выключенная морфология — строгое сужение, а не другое поведение.

        Падежи перестают распознаваться (как до DRF-1461), но ни одно
        ложное срабатывание не возвращается: обрамление работает и без
        словаря.
        """
        from goals import morphology

        settings.GOAL_SERVICE_MATCH_MORPHOLOGY = False
        morphology.reset_cache()
        try:
            assert _recognized(client_of_a, "хочу маникюр")
            assert not _recognized(client_of_a, "хочу маникюра")
            assert not _recognized(client_of_a, "нужен уход за собой")
        finally:
            morphology.reset_cache()


# ---------------------------------------------------------------------------
# DRF-1455 — обе половины салонной проверки
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSalonBoundary:
    """Решение принимается по каталогу СВОЕГО салона."""

    def test_own_salon_service_is_recognized(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Положительная половина: услуга своего салона распознаётся."""
        _salon_service(salon_a, "Маникюр")
        assert _recognized(client_of_a, "хочу маникюр")

    def test_foreign_salon_service_is_not_recognized(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Отрицательная половина — та, которой не было.

        Услуга существует ТОЛЬКО у салона B. Клиент салона A называет
        её и обязан получить уточнение, а не «цель распознана»: в его
        салоне такой услуги нет, и вести его к подбору по ней значит
        вести по признаку, которого у него не существует.
        """
        _salon_service(salon_b, "Маникюр")
        assert not _recognized(client_of_a, "хочу маникюр")

    def test_foreign_salon_category_is_not_recognized(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """То же для категорий: чужая таксономия тоже чужая."""
        ServiceCategory.objects.create(
            name="Маникюр", slug="b-manicure", tenant=salon_b,
        )
        assert not _recognized(client_of_a, "хочу маникюр")

    def test_tenantless_category_stays_shared(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Категория без салона — общая таксономия, а не чужие строки.

        ``ServiceCategory.tenant`` допускает NULL (легаси и общая
        таксономия). Такая строка не принадлежит никакому салону,
        поэтому решение по ней не является решением «по чужим строкам»,
        и отбирать её у клиента не за что: иначе пилот, где категории
        не проставлены салоном, потерял бы распознавание целиком.
        """
        ServiceCategory.objects.create(name="Маникюр", slug="g-manicure", tenant=None)
        assert _recognized(client_of_a, "хочу маникюр")

    def test_multi_provider_client_sees_only_shared_names(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Клиент с двумя салонами: салон не определён — салонные строки молчат.

        Мультипровайдерный клиент (#246) — штатный случай, и выбрать за
        него салон нельзя. Пока бот не сказал, в каком салоне человек
        сейчас, решение по салонным строкам не принимается: он получает
        уточнение и «Найти услугу», а не услугу наугад из одного из двух
        каталогов.
        """
        TenantUserRelationship.objects.create(
            user=client_of_a, tenant=salon_b, is_active=True,
            role=TenantUserRelationship.Role.CUSTOMER,
        )
        _salon_service(salon_a, "Маникюр")
        assert not _recognized(client_of_a, "хочу маникюр")

    def test_unbound_client_keeps_global_taxonomy_loses_foreign_prices(
        self, anketa_off, salon_a, salon_b, db,
    ):
        """Клиент без салона: общая таксономия жива, чужие прайсы молчат.

        Это пилотный случай. Клиент-прокси, созданный ботом, до первой
        записи не привязан ни к какому салону (#1014 выдаёт связь на
        первой записи). Салон для него неизвестен — и это честно, а не
        чинится подстановкой.

        Что при этом НЕ ломается: каноническая таксономия
        ``ServiceCategory`` заводится без салона
        (``seed_canonical_catalog``), и «Маникюр» на пилоте — именно
        такая строка. Она читается как читалась.

        Что чинится: ``SalonService`` — прайс конкретного салона. Он у
        неизвестного клиента больше не читается вовсе, и «услуга,
        которой в салоне A нет, но которая есть в салоне B» перестаёт
        давать «цель распознана».
        """
        user = User.objects.create_user(
            username="bot:matrix-unbound", password="x", role="client",
            phone="+79995000102", is_proxy=True,
        )
        ServiceCategory.objects.create(name="Маникюр", slug="g2-manicure", tenant=None)
        _salon_service(salon_b, "Наращивание ресниц")

        assert _recognized(user, "хочу маникюр")
        assert not _recognized(user, "хочу наращивание ресниц")

    def test_goal_carries_the_salon_it_was_named_in(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Салон записан на самой цели, и решение идёт по нему.

        Цель — durable-факт, и салон, в котором она названа, его часть.
        Проверяем не хранение ради хранения: строка цели, помеченная
        салоном B, судится по каталогу B даже у клиента, привязанного к
        A. Иначе поле было бы справкой, а не тем, по чему принимается
        решение.
        """
        _salon_service(salon_b, "Наращивание ресниц")

        ClientGoal.objects.filter(client=client_of_a).delete()
        goal = ClientGoal.objects.create(
            client=client_of_a,
            tenant=salon_b,
            goal_text="хочу наращивание ресниц",
            source_channel=ClientGoal.SourceChannel.BOT,
        )
        assert goal.tenant_id == salon_b.id
        assert not build_decision_context(client_of_a)["missing"]


# ---------------------------------------------------------------------------
# DRF-1455 — салон попадает на цель в момент её создания
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTenantStamping:
    """Салон проставляется один раз — когда цель названа."""

    URL = "/api/v1/internal/me/goals/select/"

    @staticmethod
    def _api(token: str, external_user_id: str, tenant_slug: str | None = None):
        from rest_framework.test import APIClient

        api = APIClient()
        api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        api.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
        if tenant_slug:
            api.defaults["HTTP_X_TENANT"] = tenant_slug
        return api

    @pytest.fixture
    def token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = "test-token-tenant-stamping"
        settings.GOAL_ANKETA_ENABLED = False
        return "test-token-tenant-stamping"

    def test_relationship_is_used_when_header_absent(
        self, token, salon_a, client_of_a,
    ):
        """Единственная активная связь клиента — источник по умолчанию."""
        api = self._api(token, client_of_a.username)
        resp = api.post(
            self.URL,
            {"goal_text": "хочу маникюр", "source_channel": "bot"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        goal = ClientGoal.objects.get(client=client_of_a, is_active=True)
        assert goal.tenant_id == salon_a.id

    def test_header_wins_over_relationship(self, token, salon_a, salon_b, client_of_a):
        """``X-Tenant`` — явное «я действую в салоне X», и оно сильнее вывода.

        Дерево ``/api/v1/internal/`` исключено из
        ``TenantContextMiddleware`` (бот не носит заголовок на каждый
        вызов), поэтому заголовок читается на месте. Как только бот
        начнёт его слать на goal-вызовах, менять здесь будет нечего.
        """
        api = self._api(token, client_of_a.username, tenant_slug=salon_b.slug)
        resp = api.post(
            self.URL,
            {"goal_text": "хочу маникюр", "source_channel": "bot"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        goal = ClientGoal.objects.get(client=client_of_a, is_active=True)
        assert goal.tenant_id == salon_b.id

    def test_unknown_slug_does_not_substitute_a_salon(
        self, token, salon_a, salon_b, client_of_a,
    ):
        """Неизвестный слаг — не повод подставить чужой салон.

        Опечатка в заголовке не должна тихо превращаться в «ну возьмём
        какой-нибудь». Заголовок с неизвестным слагом просто не даёт
        ответа, и решение принимает следующий источник — связь клиента.
        """
        api = self._api(token, client_of_a.username, tenant_slug="no-such-salon")
        resp = api.post(
            self.URL,
            {"goal_text": "хочу маникюр", "source_channel": "bot"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        goal = ClientGoal.objects.get(client=client_of_a, is_active=True)
        assert goal.tenant_id == salon_a.id
