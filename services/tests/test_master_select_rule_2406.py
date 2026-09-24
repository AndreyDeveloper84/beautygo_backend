"""DRF-2406 — выбор мастера из канона подтверждает связь правилом.

Решение владельца §77 п.30 (24.09): подтверждать было **некому** — в группе
прав ноль человек, и очередь на подтверждение росла вечно по построению. То
есть «проверка модератором» существовала только на бумаге, а следствие было
настоящим: ни одна выбранная мастером услуга не попадала в подбор.

Узлы держат четыре свойства:

* выбор даёт **подтверждённую** связь, и у подтверждения есть **автор
  (правило), версия, дата и основание** — без них это не подтверждение, а
  молчаливая смена статуса;
* строка **без привязки** к канону правилом не подтверждается;
* **чужие строки правило не трогает**: у сида `mapping_source_ref` пуст, и
  подтвердить его этим правилом значило бы написать «мастер выбрал» там, где
  мастер не выбирал;
* **имя правила не присваивает себе больше, чем правило делает** — оно
  подтверждает, что мастер услугу *выбрал*, а не что он её *оказывает*.

Чего узлы НЕ держат: что мастер услугу оказывает. Это предел решения, а не
недоработка, и он записан в `MASTER_SELECT_RULE`.
"""

from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, transaction

from services.models import SalonService, ServiceCategory, ServiceTemplate
from services.offer_selection import (
    MASTER_SELECT_RULE,
    decided_by_someone_else,
    MASTER_SELECT_RULE_VERSION,
    SOURCE_REF_PREFIX,
    rule_confirmation,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def canon_category(db) -> ServiceCategory:
    # Имя категории уникально на всю базу — отсюда суффикс.
    return ServiceCategory.objects.create(name=f"Канон {uuid.uuid4().hex[:6]}")


@pytest.fixture
def template(canon_category: ServiceCategory) -> ServiceTemplate:
    # Форма взята у соседнего узла (`test_catalog_models`), а не сочинена.
    return ServiceTemplate.objects.create(
        category=canon_category,
        name=f"Лазерная эпиляция подмышек {uuid.uuid4().hex[:6]}",
        name_short="Подмышки",
        duration_default=30,
        requires_health_check=False,
    )


def _tenant():
    from tenants.models import Tenant

    return Tenant.objects.create(slug=f"solo-{uuid.uuid4().hex[:8]}", name="Соло")


class TestSelectionIsConfirmedByTheRule:
    def test_row_carries_rule_version_date_and_basis(self, template) -> None:
        """Четыре поля провенанса, а не «просто verified».

        Статус без происхождения через месяц читается как умолчание — ровно то,
        из-за чего литерал однажды стал «проверенным фактом».
        """
        tenant = _tenant()
        row = SalonService.objects.create(
            tenant=tenant,
            template=template,
            category=template.category,
            name=template.name,
            source=SalonService.Source.MANUAL,
            mapping_source_ref=f"{SOURCE_REF_PREFIX}{uuid.uuid4()}",
            **rule_confirmation(),
        )

        assert row.mapping_status == SalonService.MappingStatus.VERIFIED
        assert row.mapping_confirmed_rule == MASTER_SELECT_RULE
        assert row.mapping_rule_version == MASTER_SELECT_RULE_VERSION
        assert row.mapping_confirmed_at is not None
        assert row.mapping_source_ref.startswith(SOURCE_REF_PREFIX)
        # Автор — правило, не человек: владелец назвал «кто ИЛИ какое правило»,
        # и оба сразу означали бы, что происхождение неизвестно точно.
        assert row.mapping_confirmed_by is None

    def test_the_gate_lets_it_into_recommendations(self, template) -> None:
        """Смысл правки: строка доходит до подбора, а не стоит в очереди.

        Положительная пара к узлам ниже: это то, ради чего решение принято.
        """
        tenant = _tenant()
        SalonService.objects.create(
            tenant=tenant,
            template=template,
            category=template.category,
            name=template.name,
            source=SalonService.Source.MANUAL,
            mapping_source_ref=f"{SOURCE_REF_PREFIX}{uuid.uuid4()}",
            **rule_confirmation(),
        )

        eligible = SalonService.objects.filter(
            tenant=tenant, mapping_status=SalonService.MappingStatus.VERIFIED
        )
        assert eligible.count() == 1


class TestWhatTheRuleRefusesToConfirm:
    def test_a_row_without_a_canon_link_cannot_be_confirmed(self, canon_category) -> None:
        """Подтверждать нечего, если связи нет, — и это держит схема.

        Не договорённость и не `clean()`: `clean()` обходится любым `update()`.
        """
        tenant = _tenant()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SalonService.objects.create(
                    tenant=tenant,
                    template=None,
                    category=canon_category,
                    name="Своя услуга вне канона",
                    source=SalonService.Source.MANUAL,
                    mapping_source_ref=f"{SOURCE_REF_PREFIX}{uuid.uuid4()}",
                    **rule_confirmation(),
                )

    def test_provenance_cannot_be_dropped_while_keeping_the_status(self, template) -> None:
        """`VERIFIED` без автора и даты не записывается вовсе.

        Это и есть смысл требования «автор, дата, основание обязательны»: без
        них статус меняется молча, и через месяц никто не скажет, кто решил.
        """
        tenant = _tenant()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SalonService.objects.create(
                    tenant=tenant,
                    template=template,
                    category=template.category,
                    name=template.name,
                    source=SalonService.Source.MANUAL,
                    mapping_source_ref=f"{SOURCE_REF_PREFIX}{uuid.uuid4()}",
                    mapping_status=SalonService.MappingStatus.VERIFIED,
                )


class TestTheRuleTouchesOnlyItsOwnRows:
    def test_seed_rows_are_not_the_rules_business(self, template) -> None:
        """У строк сида `mapping_source_ref` пуст — правило их не узнаёт.

        Подтвердить их правилом выбора мастера значило бы написать «мастер
        выбрал» там, где мастер не выбирал. Провенанс потом читают как факт,
        поэтому подлог здесь хуже незакрытой очереди. Их предмет — DRF-2408,
        со своим правилом и своей версией.
        """
        tenant = _tenant()
        seed = SalonService.objects.create(
            tenant=tenant,
            template=template,
            category=template.category,
            name=template.name,
            source=SalonService.Source.SEED,
            mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
            mapping_source_ref="",
        )

        selected = SalonService.objects.filter(
            mapping_source_ref__startswith=SOURCE_REF_PREFIX
        )
        assert seed.pk not in {row.pk for row in selected}
        # Положительная пара: отбор вообще работает — своя строка находится.
        mine = SalonService.objects.create(
            tenant=tenant,
            template=template,
            category=template.category,
            name=f"{template.name} (выбор мастера)",
            source=SalonService.Source.MANUAL,
            mapping_source_ref=f"{SOURCE_REF_PREFIX}{uuid.uuid4()}",
            **rule_confirmation(),
        )
        assert mine.pk in {row.pk for row in selected.all()}


class TestTheRuleNameDoesNotOverclaim:
    def test_rule_name_does_not_claim_the_master_performs_it(self) -> None:
        """Имя правила описывает выбор, а не оказание услуги.

        Предел решения владельца: правило подтверждает, что мастер услугу
        **выбрал**, и не проверяет, что он её **оказывает**. Имя, которое
        утверждало бы второе, сделало бы предел невидимым — а усилить
        формулировку при следующей правке ничего не стоит, проверить потом
        будет нечем. Тот же приём стоит у `grandfathered_before_lifecycle`.
        """
        assert MASTER_SELECT_RULE == "master_selected_from_canon"
        assert "select" in MASTER_SELECT_RULE
        for overclaim in ("perform", "provides", "checked", "human", "verified_by"):
            assert overclaim not in MASTER_SELECT_RULE

    def test_migration_rule_matches_the_live_rule(self) -> None:
        """Миграция дублирует литералы намеренно — расхождение сторожится здесь.

        Миграция не вправе зависеть от кода приложения: его переименуют, а
        миграция должна остаться накатываемой. Устранить дубль нельзя, поэтому
        он под сторожем: разойдутся — половина строк окажется подтверждена
        «каким-то правилом без версии».
        """
        import importlib

        migration = importlib.import_module("services.migrations.0027_master_select_verified")

        assert migration.RULE == MASTER_SELECT_RULE
        assert migration.RULE_VERSION == MASTER_SELECT_RULE_VERSION
        assert migration.SOURCE_REF_PREFIX == SOURCE_REF_PREFIX


class TestRemovalKeepsItsMeaning:
    """«Убрать из моих услуг» не должно поменять смысл от этой правки.

    До DRF-2406 удаление не стирало строку с решённой связью. Если бы признаком
    осталось одно лишь `VERIFIED`, то после правки решённой стала бы каждая
    выбранная строка — и кнопка перестала бы удалять что-либо вовсе. Лист менял
    очередь на подтверждение, а не смысл кнопки.

    Граница проходит не между человеком и правилом: подтверждать умеет и другое
    правило (`owner_review`), и его решение — чужое. Граница — чьё решение
    держит строку.
    """

    def _row(self, tenant, template, **over):
        fields = {
            "tenant": tenant,
            "template": template,
            "category": template.category,
            "name": template.name,
            "source": SalonService.Source.MANUAL,
            "mapping_source_ref": f"{SOURCE_REF_PREFIX}{uuid.uuid4()}",
            **rule_confirmation(),
        }
        fields.update(over)
        return SalonService.objects.create(**fields)

    def test_the_masters_own_selection_is_still_deletable(self, template) -> None:
        """Отозвать собственный выбор мастер может — как и до правки."""
        assert decided_by_someone_else(self._row(_tenant(), template)) is False

    def test_another_rules_decision_is_held(self, template) -> None:
        """Чужое правило — чужое решение: строку держим.

        Это тот случай, который уже стерёгся узлом M8b: подтверждать связь
        правилом умеет не только выбор мастера.
        """
        row = self._row(
            _tenant(),
            template,
            mapping_confirmed_rule="owner_review",
            mapping_rule_version="1",
            mapping_source_ref="review-1800b",
        )

        assert decided_by_someone_else(row) is True

    def test_a_human_decision_is_held(self, template, django_user_model) -> None:
        moderator = django_user_model.objects.create(username=f"mod-{uuid.uuid4().hex[:8]}")
        row = self._row(
            _tenant(),
            template,
            mapping_confirmed_by=moderator,
            mapping_confirmed_rule="",
            mapping_rule_version="",
        )

        assert decided_by_someone_else(row) is True

    def test_a_refusal_is_held_whoever_signed_it(self, template) -> None:
        """Отказ правило выбора не ставит никогда — значит решил кто-то другой.

        Провенанс здесь намеренно оставлен от правила выбора: такая строка в
        жизни не встречается, и узел проверяет, что разбор опирается на статус,
        а не на имя в провенансе. Иначе невозможная строка проходила бы как
        «своя» и удалялась вместе с чужим решением.
        """
        row = self._row(
            _tenant(),
            template,
            mapping_status=SalonService.MappingStatus.NOT_RECOMMENDABLE,
        )

        assert decided_by_someone_else(row) is True
