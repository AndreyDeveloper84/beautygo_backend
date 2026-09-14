"""Живой silent-remap на пилоте закрыт (DRF-1668, MAP-AUTO-02b, план C-9).

Повторный confirm YClients-драфта перезаписывал `template` (в том числе в
NULL) у существующей `SalonService`, `mapping_status` не трогая, а схема
пропускала `VERIFIED + template NULL`. Formula Tela — единственный
YClients-салон, и владелец размечает его руками: этот путь стирал бы его
разметку без следа.

Три сторожа, каждый с положительной парой:

1. схема: `VERIFIED` ⇒ `template NOT NULL` (`salonservice_verified_requires_template`);
2. форма §76: отказ по полю `template`, не именем ограничения;
3. confirm: у решённой услуги (`VERIFIED` / `NOT_RECOMMENDABLE`) `template`
   не перезаписывается — именованный отказ, драфт остаётся, счётчик.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.admin import SalonServiceAdminForm
from services.integrations.intake import confirm as confirm_mod
from services.integrations.intake.confirm import DraftNotConfirmable, confirm_draft
from services.models import (
    DraftSalonService,
    ExternalSourceMapping,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
)
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

S = SalonService.MappingStatus


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="formula-tela-1668", name="Formula Tela")


@pytest.fixture
def category():
    return ServiceCategory.objects.create(name="Массаж 1668")


@pytest.fixture
def template(category):
    return ServiceTemplate.objects.create(
        category=category, name="Классический массаж", name_short="Массаж",
        duration_default=60, requires_health_check=False,
    )


@pytest.fixture
def other_template(category):
    return ServiceTemplate.objects.create(
        category=category, name="Лимфодренажный массаж", name_short="Лимфо",
        duration_default=60, requires_health_check=False,
    )


@pytest.fixture
def owner():
    return User.objects.create_user(username="owner-1668", password="x", role="admin")  # pragma: allowlist secret


def _draft(tenant, template, *, eid="1668", name="Массаж классический"):
    return DraftSalonService.objects.create(
        tenant=tenant,
        external_source=DraftSalonService.ExternalSource.YCLIENTS,
        external_service_id=eid, external_name=name,
        suggested_template=template, suggested_duration=60,
        suggested_price=Decimal("1500"), raw_payload={"id": eid},
    )


def _redraft(draft, template, *, name=None):
    """Повторный импорт из YClients обновляет ТОТ ЖЕ драфт (уникальность по
    (tenant, source, external_service_id)): другой предложенный шаблон,
    другое имя — и снова confirm."""
    draft.suggested_template = template
    if name is not None:
        draft.external_name = name
    draft.status = DraftSalonService.Status.PENDING
    draft.save()
    return draft


def _decide(salon, status, owner):
    """Решение владельца — как его ставит форма/verify_pilot_slice."""
    salon.mapping_status = status
    salon.mapping_confirmed_by = owner
    salon.mapping_confirmed_at = timezone.now()
    salon.mapping_source_ref = "OWNER_TASK_MAPPING_PILOT_SLICE_2026-09-12.md"
    salon.save()
    return salon


class TestSchema:
    def test_verified_without_template_is_refused_by_the_database(self, tenant, category, template, owner):
        salon = SalonService.objects.create(
            tenant=tenant, template=template, name="С шаблоном", duration_minutes=60,
        )
        _decide(salon, S.VERIFIED, owner)
        # Положительная пара: VERIFIED с шаблоном — на месте.
        assert SalonService.objects.get(pk=salon.pk).mapping_status == S.VERIFIED

        # Обход clean() — update(): раньше проходил молча.
        with pytest.raises(IntegrityError, match="salonservice_verified_requires_template"), transaction.atomic():
            SalonService.objects.filter(pk=salon.pk).update(template=None, category=category)
        assert SalonService.objects.get(pk=salon.pk).template_id == template.pk

    def test_not_recommendable_may_have_no_template(self, tenant, category, owner):
        """«Решено, что связи НЕ БУДЕТ» — у отказа шаблона может и не быть."""
        salon = SalonService.objects.create(
            tenant=tenant, template=None, category=category, name="Без канона", duration_minutes=60,
        )
        _decide(salon, S.NOT_RECOMMENDABLE, owner)
        assert SalonService.objects.get(pk=salon.pk).template_id is None


class TestForm:
    def _form(self, salon, owner, **over):
        data = {
            "tenant": salon.tenant_id, "name": salon.name, "duration_minutes": 60,
            "template": salon.template_id, "category": "", "is_active": True,
            "mapping_status": S.VERIFIED, "mapping_confirmed_by": owner.pk,
            "mapping_confirmed_at": timezone.now(), "mapping_source_ref": "разбор",
            "source": SalonService.Source.MANUAL, "mapping_confirmed_rule": "", "mapping_rule_version": "",
            "requires_health_check": "",
        }
        data.update(over)
        return SalonServiceAdminForm(data=data, instance=salon)

    def test_form_explains_by_field(self, tenant, category, template, owner):
        salon = SalonService.objects.create(
            tenant=tenant, template=template, name="Ф", duration_minutes=60,
        )
        ok = self._form(salon, owner)
        assert "template" not in ok.errors, ok.errors  # положительная пара
        bad = self._form(salon, owner, template="", category=category.pk)
        assert not bad.is_valid()
        assert "template" in bad.errors
        assert any(e.code == "verified_requires_template" for e in bad.errors.as_data()["template"])
        assert "__all__" not in bad.errors or "verified_requires_template" not in str(bad.errors["__all__"])


class TestConfirmDoesNotRemapDecidedRows:
    @pytest.fixture
    def verified(self, tenant, template, owner):
        draft = _draft(tenant, template)
        confirm_draft(draft, fallback_category=None)
        salon = ExternalSourceMapping.objects.get(external_id="1668").salon_service
        return draft, _decide(salon, S.VERIFIED, owner)

    def test_reconfirm_with_a_different_template_is_refused_and_row_untouched(
        self, other_template, verified, category
    ):
        draft, salon = verified
        before = SalonService.objects.get(pk=salon.pk)
        _redraft(draft, other_template, name="Переименовали в YClients")
        with pytest.raises(DraftNotConfirmable, match="decided_template_remap"):
            confirm_draft(draft, fallback_category=category)
        after = SalonService.objects.get(pk=salon.pk)
        assert after.template_id == before.template_id
        assert after.name == before.name and after.mapping_status == S.VERIFIED
        draft.refresh_from_db()
        assert draft.status == DraftSalonService.Status.PENDING  # драфт остался

    def test_reconfirm_with_no_template_is_refused_too(self, verified, category):
        draft, salon = verified
        _redraft(draft, None)
        with pytest.raises(DraftNotConfirmable, match="decided_template_remap"):
            confirm_draft(draft, fallback_category=category)
        assert SalonService.objects.get(pk=salon.pk).template_id == salon.template_id

    def test_reconfirm_with_the_same_template_still_updates_the_rest(self, template, verified):
        """Положительная пара: тот же шаблон — обычный идемпотентный re-confirm."""
        draft, salon = verified
        _redraft(draft, template, name="Массаж классический (60 мин)")
        result = confirm_draft(draft, fallback_category=None)
        assert not result.created_salon_service
        after = SalonService.objects.get(pk=salon.pk)
        assert after.name == "Массаж классический (60 мин)"
        assert after.template_id == template.pk and after.mapping_status == S.VERIFIED

    def test_undecided_row_is_still_remappable(self, tenant, template, other_template):
        """У UNMAPPED/REVIEW_REQUIRED confirm по-прежнему ведёт шаблон за драфтом."""
        draft = _draft(tenant, template)
        confirm_draft(draft, fallback_category=None)
        salon = ExternalSourceMapping.objects.get(external_id="1668").salon_service
        assert salon.mapping_status == S.UNMAPPED
        confirm_draft(_redraft(draft, other_template), fallback_category=None)
        assert SalonService.objects.get(pk=salon.pk).template_id == other_template.pk

    def test_not_recommendable_is_decided_too(self, tenant, template, other_template, owner):
        draft = _draft(tenant, template)
        confirm_draft(draft, fallback_category=None)
        salon = ExternalSourceMapping.objects.get(external_id="1668").salon_service
        _decide(salon, S.NOT_RECOMMENDABLE, owner)
        with pytest.raises(DraftNotConfirmable, match="decided_template_remap"):
            confirm_draft(_redraft(draft, other_template), fallback_category=None)
        assert SalonService.objects.get(pk=salon.pk).template_id == template.pk

    def test_refusal_is_counted_and_logged_with_a_name(self, other_template, verified, category):
        """Логгер `services` в настройках не пропагирует в root — caplog его
        не видит; смотрим на сам вызов `logger.error`."""
        draft, _salon = verified
        before = confirm_mod.REMAP_REFUSED.value
        with patch.object(confirm_mod.logger, "error") as error, pytest.raises(DraftNotConfirmable):
            confirm_draft(_redraft(draft, other_template), fallback_category=category)
        assert confirm_mod.REMAP_REFUSED.value == before + 1
        error.assert_called_once()
        assert "decided_template_remap" in error.call_args.args[0]
