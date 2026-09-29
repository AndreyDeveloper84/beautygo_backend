"""DRF-2612 — указатель провенанса, который CHECK запрещает обнулять, не SET_NULL.

До листа у всех указателей провенанса каталога стояло ``on_delete=SET_NULL``,
а CHECK тех же моделей требует поле непустым у подтверждённой строки. Сегодня
``User`` физически не удаляется (стирание — tombstone), и несовместимость не
наступала; первое настоящее удаление дало бы ``IntegrityError`` посреди
исполнителя стирания.

**Предел:** физического удаления ``User`` в системе сегодня нет — узел на
живую пару ниже проверяет путь, которым система пока не ходит. Он держит то,
что этот путь, когда появится, откажет названо, а не упадёт на CHECK.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import ProtectedError
from django.utils import timezone

from users.deletion_executor import set_null_pointers_required_by_check

User = get_user_model()


def _field(label: str):
    from django.apps import apps

    app_model, name = label.rsplit(".", 1)
    return apps.get_model(app_model)._meta.get_field(name)


class TestTheGuard:
    def test_no_pointer_is_both_nullable_on_delete_and_required_by_check(self):
        # Положительная стража переписи: SET_NULL у указателей на User есть,
        # сторож смотрит туда, где они живут.
        assert _field("appointments.Appointment.cancelled_by").remote_field.on_delete is models.SET_NULL
        # empty-assert-ok: SET_NULL у указателей на User есть — строкой выше
        assert set_null_pointers_required_by_check() == set()

    def test_setting_one_back_to_set_null_is_named(self, monkeypatch):
        field = _field("services.ServiceTemplate.approved_by")
        monkeypatch.setattr(field.remote_field, "on_delete", models.SET_NULL)
        assert set_null_pointers_required_by_check() == {"services.ServiceTemplate.approved_by"}

    def test_lawful_set_null_without_a_check_is_left_alone(self):
        """Вторая половина: обнуление там, где NULL допустим, — решение, не
        ловушка. Акторы истории, журнал доступа и черновик разбора — SET_NULL
        без CHECK на поле, и сторож их не трогает."""
        lawful = {
            "services.DraftSalonService.confirmed_by",
            "appointments.Appointment.cancelled_by",
            "appointments.AppointmentRevision.actor",
            "privacy_audit.PersonalDataAccessLog.actor",
        }
        for label in lawful:
            assert _field(label).remote_field.on_delete is models.SET_NULL, label
        # empty-assert-ok: все четыре — SET_NULL, строками выше
        assert lawful & set_null_pointers_required_by_check() == set()


@pytest.mark.django_db
class TestDeletingAReviewer:
    """Пара, которая обязана различаться: удаление учётки С подтверждённым
    провенансом — названный отказ; БЕЗ него — проходит. «Удаление не падает»
    прошло бы и при снятом CHECK."""

    def _synonym_confirmed_by(self, user):
        from services.models import ServiceCategory, ServiceTemplate, ServiceTemplateSynonym

        category = ServiceCategory.objects.create(name="Ногти 2612", slug="nails-2612")
        template = ServiceTemplate.objects.create(
            category=category, name="Маникюр 2612", name_short="Маникюр", duration_default=60
        )
        return ServiceTemplateSynonym.objects.create(
            template=template,
            text="маник",
            confirmed_by=user,
            confirmed_at=timezone.now(),
            source_ref="drf-2612",
        )

    def test_a_reviewer_with_confirmed_provenance_is_refused_by_name(self):
        reviewer = User.objects.create_user(username="reviewer-2612", password="x")
        synonym = self._synonym_confirmed_by(reviewer)
        with pytest.raises(ProtectedError) as exc:
            reviewer.delete()
        assert synonym in exc.value.protected_objects
        synonym.refresh_from_db()
        assert synonym.confirmed_by_id == reviewer.pk

    def test_a_user_without_provenance_is_deleted(self):
        plain = User.objects.create_user(username="plain-2612", password="x")
        pk = plain.pk
        plain.delete()
        assert User.objects.filter(pk=pk).count() == 0
