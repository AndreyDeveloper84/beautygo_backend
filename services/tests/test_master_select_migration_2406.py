"""DRF-2406 — разовый прогон правила по уже выбранным строкам.

Главное свойство прогона — **не трогать чужое**. Оно проверяется здесь, потому
что в узлах модели его проверить нечем: там нет ни отбора строк, ни самого
прогона.

### Почему состояние базы не откатывается

Миграция 0027 **не меняет схему** — это данные. Схема на 0026 и на 0027 одна и
та же, поэтому откатывать её незачем: узел заводит строки живыми моделями и
зовёт функцию прогона напрямую.

Первая редакция откатывала `services` на 0026 и брала исторические модели — и
упёрлась в то, что стоит запомнить: **назад откатывается одно приложение, а
колонки остальных остаются**. Историческая модель тенанта не знала поля `kind`,
а колонка в базе требовала его NOT NULL. Отката не требовалось вовсе.

Что проверяется накаткой в CI, а не здесь: что миграция вообще применяется и
что её зависимость названа верно. Это делает `migrate` на чистой базе.
"""

from __future__ import annotations

import importlib
import uuid

import pytest
from django.apps import apps as live_apps
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate
from services.offer_selection import MASTER_SELECT_RULE, MASTER_SELECT_RULE_VERSION
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("services.migrations.0027_master_select_verified")


@pytest.fixture
def canon():
    category = ServiceCategory.objects.create(name=f"Канон {uuid.uuid4().hex[:6]}")
    template = ServiceTemplate.objects.create(
        category=category,
        name=f"Эпиляция {uuid.uuid4().hex[:6]}",
        name_short="Эпиляция",
        duration_default=30,
        requires_health_check=False,
    )
    tenant = Tenant.objects.create(
        slug=f"solo-{uuid.uuid4().hex[:8]}", name="Соло", kind=Tenant.Kind.SOLO
    )
    return tenant, category, template


def _row(tenant, category, template, **over):
    fields = {
        "tenant": tenant,
        "template": template,
        "category": category,
        "name": f"{template.name} {uuid.uuid4().hex[:4]}",
        "source": SalonService.Source.MANUAL,
        "mapping_status": SalonService.MappingStatus.REVIEW_REQUIRED,
        "mapping_source_ref": f"master_select:{uuid.uuid4()}",
    }
    fields.update(over)
    return SalonService.objects.create(**fields)


class TestTheOneOffRun:
    def test_it_confirms_a_selection_with_rule_version_and_date(self, canon) -> None:
        tenant, category, template = canon
        row = _row(tenant, category, template)

        MIGRATION.confirm_master_selections(live_apps, None)

        row.refresh_from_db()
        assert row.mapping_status == SalonService.MappingStatus.VERIFIED
        assert row.mapping_confirmed_rule == MASTER_SELECT_RULE
        assert row.mapping_rule_version == MASTER_SELECT_RULE_VERSION
        assert row.mapping_confirmed_at is not None
        assert row.mapping_confirmed_by is None

    def test_a_seed_row_is_not_the_rules_business(self, canon) -> None:
        """У сида основание пусто — «мастер выбрал» о нём неправда.

        Это не осторожность: провенанс потом читают как факт, и подлог здесь
        хуже незакрытой очереди. 206 строк сида закрываются своим правилом со
        своей версией — DRF-2408.
        """
        tenant, category, template = canon
        seed = _row(
            tenant, category, template, source=SalonService.Source.SEED, mapping_source_ref=""
        )
        mine = _row(tenant, category, template)

        MIGRATION.confirm_master_selections(live_apps, None)

        seed.refresh_from_db()
        mine.refresh_from_db()
        # Положительная пара в том же прогоне: своя строка подтверждена, чужая — нет.
        assert mine.mapping_status == SalonService.MappingStatus.VERIFIED
        assert seed.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
        assert seed.mapping_confirmed_rule == ""

    def test_a_row_of_unknown_origin_is_not_touched(self, canon) -> None:
        """Отбор положительный: «только свои», а не «все, кроме известных чужих».

        На стенде 24.09 нашлась группа, которой никто не ждал: 206 строк сида —
        это не 171 строка генератора демо, ещё 35 лежат в салонах `mkt-*`,
        созданных 23.08, и чем они заведены — не измерено. Отрицательный отбор
        («всё, кроме демо») такую группу пропустил бы и подтвердил бы её
        правилом выбора мастера, то есть соврал бы о происхождении.

        Узел держит именно это: строка с **чужим** основанием не трогается,
        каким бы оно ни было — пустым, как у сида, или незнакомым.
        """
        tenant, category, template = canon
        stranger = _row(tenant, category, template, mapping_source_ref="mkt-import:2026-08-23")
        mine = _row(tenant, category, template)

        MIGRATION.confirm_master_selections(live_apps, None)

        stranger.refresh_from_db()
        mine.refresh_from_db()
        # Положительная пара в том же прогоне: своя строка подтверждена.
        assert mine.mapping_status == SalonService.MappingStatus.VERIFIED
        assert stranger.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
        assert stranger.mapping_confirmed_rule == ""

    def test_a_row_without_a_canon_link_is_skipped(self, canon) -> None:
        """Подтверждать нечего — и схема такую строку всё равно не примет."""
        tenant, category, template = canon
        # Без привязки: `template=None` подставляется через набор полей, иначе
        # он спорит с позиционным аргументом помощника.
        unlinked = _row(tenant, category, template)
        SalonService.objects.filter(pk=unlinked.pk).update(template=None)
        unlinked.refresh_from_db()

        MIGRATION.confirm_master_selections(live_apps, None)

        unlinked.refresh_from_db()
        assert unlinked.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED

    def test_someone_elses_confirmation_is_not_rewritten(self, canon) -> None:
        """Чужое правило уже подтвердило — своё имя поверх не пишем."""
        tenant, category, template = canon
        already = _row(
            tenant,
            category,
            template,
            mapping_status=SalonService.MappingStatus.VERIFIED,
            mapping_confirmed_rule="owner_review",
            mapping_rule_version="1",
            mapping_confirmed_at=timezone.now(),
            mapping_source_ref="review-1800b",
        )

        MIGRATION.confirm_master_selections(live_apps, None)

        already.refresh_from_db()
        assert already.mapping_confirmed_rule == "owner_review"
        assert already.mapping_source_ref == "review-1800b"

    def test_the_run_is_idempotent(self, canon) -> None:
        """Второй прогон ничего не меняет: дата подтверждения та же.

        Прогон может повториться — накаткой на другой базе, повторным
        применением. Если бы он переписывал дату, «когда подтвердили» стало бы
        «когда последний раз накатывали».
        """
        tenant, category, template = canon
        row = _row(tenant, category, template)

        MIGRATION.confirm_master_selections(live_apps, None)
        row.refresh_from_db()
        first = row.mapping_confirmed_at

        MIGRATION.confirm_master_selections(live_apps, None)
        row.refresh_from_db()

        assert row.mapping_confirmed_at == first


class TestTheRuleLiteralsCannotDrift:
    def test_migration_repeats_the_live_rule(self) -> None:
        """Миграция дублирует литералы намеренно — расхождение под сторожем.

        Миграция не вправе зависеть от кода приложения: его переименуют, а она
        должна остаться накатываемой. Устранить дубль нельзя, поэтому он
        стережётся: разойдутся — часть строк окажется подтверждена «каким-то
        правилом без версии».
        """
        from services.offer_selection import SOURCE_REF_PREFIX

        assert MIGRATION.RULE == MASTER_SELECT_RULE
        assert MIGRATION.RULE_VERSION == MASTER_SELECT_RULE_VERSION
        assert MIGRATION.SOURCE_REF_PREFIX == SOURCE_REF_PREFIX


class TestTheHalfFilledRowDoesNotAbortTheRun:
    def test_a_human_in_provenance_on_a_queued_row_is_replaced_not_fatal(self, canon) -> None:
        """Строка «в очереди» с заполненным «кто» роняла бы накатку целиком.

        До неё доводит сама очередь: администратор вписал автора и не сменил
        статус. Схема запрещает «и человек, и правило»
        (`salonservice_provenance_is_who_xor_rule`), поэтому прогон падал бы на
        середине — а падает он внутри миграции, то есть роняет и развёртывание.

        По §77 п.30 автор теперь правило, значит половинчатый человек снимается,
        и провенанс говорит правду о том, кто решил.
        """
        from django.contrib.auth import get_user_model

        tenant, category, template = canon
        moderator = get_user_model().objects.create(username=f"mod-{uuid.uuid4().hex[:8]}")
        row = _row(tenant, category, template, mapping_confirmed_by=moderator)

        MIGRATION.confirm_master_selections(live_apps, None)

        row.refresh_from_db()
        assert row.mapping_status == SalonService.MappingStatus.VERIFIED
        assert row.mapping_confirmed_rule == MASTER_SELECT_RULE
        assert row.mapping_confirmed_by_id is None


class TestTheDriftGuardWatchesFieldsToo:
    def test_the_run_writes_every_field_the_live_path_writes(self) -> None:
        """Сторож на литералы пропустил бы новое поле провенанса.

        Добавить пятое поле в `rule_confirmation` и забыть его в миграции —
        значит подтвердить часть строк неполным провенансом, и ни один узел не
        покраснеет, пока сверяются только имя и версия.
        """
        import inspect

        from services.offer_selection import rule_confirmation

        source = inspect.getsource(MIGRATION.confirm_master_selections)
        # `mapping_source_ref` — исключение по существу: у живой строки основание
        # своё (кто выбрал), а прогон закрывает уже существующие, где оно стоит.
        live = set(rule_confirmation(source_ref="master_select:x")) - {"mapping_source_ref"}

        missing = sorted(field for field in live if field not in source)

        assert live, "положительная пара: помощник вообще возвращает поля"
        assert missing == [], f"миграция не пишет {missing}, а живой путь пишет"
