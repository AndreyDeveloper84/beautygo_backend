"""Матрица распознавания названной услуги — DRF-1461 и DRF-1472.

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

Вторая половина устроена так же, но граница у неё НЕ салонная
(DRF-1472, владелец 04.09.2026). Услуга, которая есть хоть у одного
салона, распознаётся; имя, которого нет ни у одного, — не
распознаётся и получает уточнение с выходом «Найти услугу».

Салонная граница DRF-1455 отменена, и проверки на неё ниже переписаны,
а не удалены: они утверждали поведение («клиент салона A не должен
распознать услугу салона B»), которое владелец отменил, — и их
утверждение теперь ровно обратное. Удалить их значило бы оставить
отмену без стражи с обеих сторон, а именно её отсутствие и пропустило
оба прежних дефекта до слияния.

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
    # Имя во МНОЖЕСТВЕННОМ числе — нарочно. В замере DRF-1461
    # «стрижка» не распозналась, и причина не в матчинге как таковом:
    # каталог хранит рубрику своим именем, а человек пишет своим. Без
    # морфологии это две разные строки; с ней — одна словарная форма.
    ServiceCategory.objects.create(name="Стрижки", slug="m-haircut", tenant=salon_a)
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
    ("хочу стрижку", "винительный к имени каталога во мн. числе"),
    ("стрижка", "строка замера: каталог хранит «Стрижки», человек пишет «стрижка»"),
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
# DRF-1472 — граница осталась одна, и она не салонная
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGlobalCatalog:
    """Решение принимается по каталогу ВСЕХ салонов.

    Прежняя редакция этого класса (``TestSalonBoundary``, DRF-1455)
    требовала обратного: клиент салона A обязан был НЕ распознать
    услугу салона B. Владелец 04.09.2026 отменил это правило,
    разобравшись, как устроены боты: цели спрашивают только в
    клиентском боте — он один, общий, и салон у него не задан нарочно,
    это витрина. Салонный бот обслуживает владельцев салонов и целей не
    спрашивает, поэтому случая «клиент пришёл через бота салона» не
    существует.

    Проверки переписаны утверждением наизнанку, а не выброшены: тогда
    отмена остаётся проверяемой, и вернуть салонную границу молча
    нельзя.
    """

    def test_service_of_any_salon_is_recognized(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Услуга есть ТОЛЬКО у салона B — клиент салона A её называет.

        Это ровно тот случай, который DRF-1455 запрещал. Теперь он
        обязателен: витрина показывает все салоны, и услуга, которая
        где-то есть, человеку доступна. Довести его до салона, у
        которого она есть, — уже дело подбора.
        """
        _salon_service(salon_b, "Наращивание ресниц")
        assert _recognized(client_of_a, "хочу наращивание ресниц")

    def test_category_of_any_salon_is_recognized(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """То же для категорий: чужой таксономии больше не бывает."""
        ServiceCategory.objects.create(
            name="Наращивание ресниц", slug="b-lashes", tenant=salon_b,
        )
        assert _recognized(client_of_a, "хочу наращивание ресниц")

    def test_service_of_own_salon_is_recognized(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Положительная половина не пострадала от расширения."""
        _salon_service(salon_a, "Маникюр")
        assert _recognized(client_of_a, "хочу маникюр")

    def test_tenantless_category_stays_shared(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Категория без салона читается как читалась.

        ``ServiceCategory.tenant`` допускает NULL (общая/легаси
        таксономия). На пилоте именно такие строки — «Маникюр», «Уход»,
        «Стрижки» — покрывают почти весь замер DRF-1461, и проверка
        стоит здесь, чтобы расширение каталога не оказалось заодно и
        подменой источника.
        """
        ServiceCategory.objects.create(name="Маникюр", slug="g-manicure", tenant=None)
        assert _recognized(client_of_a, "хочу маникюр")

    def test_multi_provider_client_sees_the_whole_catalog(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Клиент с двумя салонами больше не теряет распознавание.

        DRF-1455 на нём молчал: выбрать за мультипровайдерного клиента
        (#246) один салон нельзя, и салонные строки для него не
        читались вовсе. Теперь выбирать нечего — каталог один.
        """
        TenantUserRelationship.objects.create(
            user=client_of_a, tenant=salon_b, is_active=True,
            role=TenantUserRelationship.Role.CUSTOMER,
        )
        _salon_service(salon_a, "Наращивание ресниц")
        assert _recognized(client_of_a, "хочу наращивание ресниц")

    def test_unbound_client_sees_the_whole_catalog(
        self, anketa_off, salon_a, salon_b, db,
    ):
        """Клиент без единой связи с салоном — и есть пилотный случай.

        Клиент-прокси, созданный ботом, до первой записи не привязан ни
        к какому салону (#1014 выдаёт связь на первой записи). При
        DRF-1455 такой человек — то есть КАЖДЫЙ до первой записи — не
        видел ни одного прайса. Именно это владелец и отменил: витрина
        обязана отвечать до того, как человек где-то записался.
        """
        user = User.objects.create_user(
            username="bot:matrix-unbound", password="x", role="client",
            phone="+79995000102", is_proxy=True,
        )
        ServiceCategory.objects.create(name="Маникюр", slug="g2-manicure", tenant=None)
        _salon_service(salon_b, "Наращивание ресниц")

        assert _recognized(user, "хочу маникюр")
        assert _recognized(user, "хочу наращивание ресниц")

    def test_repeated_names_across_salons_do_not_eat_the_cap(
        self, anketa_off, salon_a, salon_b, client_of_a, monkeypatch,
    ):
        """Одно имя у двух салонов — это одно имя, а не два.

        ``MAX_CATALOG_NAMES`` — предохранитель от последовательного
        чтения тысяч строк зеркала. Пока каталог читался по одному
        салону, повторов почти не было; на общем каталоге без
        ``distinct()`` потолок съедали бы копии одного и того же имени,
        и услуги, до которых очередь не дошла, переставали бы
        распознаваться — тихо и в зависимости от того, кто как назвал
        свою услугу.

        Расклад собран руками, а не помощником ``_salon_service``:
        потолок здесь опущен до трёх, и служебные категории-держатели,
        которые помощник заводит на каждый салон, съели бы его сами.

        Одна общая категория (1 имя) оставляет под услуги 2 места. Без
        ``distinct()`` туда попадают два «Маникюра» и «Педикюр»
        теряется; с ним — «Маникюр» и «Педикюр», как и должно быть.
        """
        from goals import service_match

        monkeypatch.setattr(service_match, "MAX_CATALOG_NAMES", 3)
        shared = ServiceCategory.objects.create(
            name="Общее", slug="cap-shared", tenant=None,
        )
        for tenant, name in (
            (salon_a, "Маникюр"), (salon_b, "Маникюр"), (salon_b, "Педикюр"),
        ):
            SalonService.objects.create(
                tenant=tenant, name=name, category=shared, template=None,
                is_active=True,
            )

        assert _recognized(client_of_a, "хочу педикюр")

    def test_name_no_salon_has_is_not_recognized(
        self, anketa_off, salon_a, salon_b, client_of_a,
    ):
        """Единственная оставшаяся граница — и она не салонная.

        Услуги, которой нет НИ У ОДНОГО салона, не существует и для
        распознавания. Человек получает уточнение и выход «Найти
        услугу» — то есть доходит до подбора другим путём, а не
        оказывается заперт.

        Без этой проверки «ищем по всем салонам» неотличимо от «ищем
        везде и всегда находим»: остальные проверки класса умеют только
        подтверждать распознавание.
        """
        _salon_service(salon_a, "Маникюр")
        _salon_service(salon_b, "Педикюр")
        assert not _recognized(client_of_a, "хочу наращивание ресниц")


# ---------------------------------------------------------------------------
# DRF-1472 — салон на цель не попадает вовсе
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGoalCarriesNoSalon:
    """Цель не несёт салона — ни колонкой, ни решением.

    Прежняя редакция (``TestTenantStamping``, DRF-1455) проверяла три
    источника, из которых салон попадал на цель: заголовок ``X-Tenant``,
    единственная активная связь клиента, легаси ``User.tenant``. Все три
    сняты вместе с колонкой, и проверки переписаны в утверждение, что
    салон на решение больше не влияет НИКАК. Иначе тихий возврат любого
    из трёх источников никто бы не заметил.
    """

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
        """Анкета включена — под тем же флагом живёт и распознавание.

        ``GOAL_SERVICE_MATCH_MORPHOLOGY`` тоже включаем явно: проверки
        ниже читают документ состояния целиком, а не одну колонку, и
        зависеть от умолчания настройки им незачем.
        """
        settings.AYLA_INTERNAL_API_TOKEN = "test-token-no-tenant"
        settings.GOAL_ANKETA_ENABLED = True
        settings.GOAL_SERVICE_MATCH_MORPHOLOGY = True
        return "test-token-no-tenant"

    def test_clientgoal_has_no_tenant_column(self):
        """Колонки нет в модели — не «есть, но не заполняется».

        Поле, оставленное «на всякий случай», рано или поздно начинает
        влиять: кто-нибудь прочитает его в фильтре и вернёт салонную
        границу, не заметив, что возвращает.
        """
        names = {f.name for f in ClientGoal._meta.get_fields()}
        assert "tenant" not in names

    def test_header_does_not_change_the_decision(
        self, token, salon_a, salon_b, client_of_a,
    ):
        """``X-Tenant`` чужого салона ничего не меняет.

        Заголовок — единственный явный способ сказать «я в салоне X», и
        именно он раньше решал. Клиент называет услугу салона A, придя
        с заголовком салона B, и обязан быть распознан: салон в решении
        не участвует.
        """
        _salon_service(salon_a, "Наращивание ресниц")
        api = self._api(token, client_of_a.username, tenant_slug=salon_b.slug)
        resp = api.post(
            self.URL,
            {"goal_text": "хочу наращивание ресниц", "source_channel": "bot"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["data"]["missing"] == []

    def test_service_of_a_salon_the_client_never_visited_is_recognized(
        self, token, salon_a, salon_b, client_of_a,
    ):
        """Сквозной путь целиком: витрина отвечает по чужому прайсу.

        Клиент связан только с салоном A, заголовка нет — то есть все
        прежние источники салона указывали бы на A. Услуга есть только
        у B. Владелец распорядился прямо: показываем все салоны,
        включая те, где человек никогда не был.
        """
        _salon_service(salon_b, "Наращивание ресниц")
        api = self._api(token, client_of_a.username)
        resp = api.post(
            self.URL,
            {"goal_text": "хочу наращивание ресниц", "source_channel": "bot"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["data"]["missing"] == []
        assert ClientGoal.objects.filter(client=client_of_a, is_active=True).exists()
