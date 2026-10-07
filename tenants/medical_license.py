"""`MedicalLicense` — медицинская лицензия салона (Body Care §7A-2, DRF-2838).

Контракт v0.2 §7A.3: предложение класса ``MEDICAL_COSMETOLOGY`` не может быть
готово, пока у салона не подтверждены лицензия, её номер, объём и адреса.

Почему сущность салона, а не поля предложения
---------------------------------------------

Контракт кладёт ``tenant_medical_license_status``, ``license_ref``,
``license_service_scope``, ``licensed_address`` на ``SalonOffering``. Но
лицензия — факт юрлица: одна лицензия покрывает много услуг и несколько
адресов. Поля на каждой услуге разъехались бы по сотне строк. Поэтому
лицензия — своя строка у салона, а состояние лицензии предложения
**выводится** (``services.body_care_license``), а не вводится (решение
главного окна по A2(1), 06.10).

Что значит «проверена»
----------------------

Отдельного статуса нет: лицензия проверена, когда у неё есть провенанс
проверки — кто, когда, основание (CheckConstraint «все три или ни одного»).
Проверку ставит только человек; кто вправе — владелец / юрист (вопрос A2(2)
владельцу), база этого не решает.

Объём и адреса
--------------

* ``covered_templates`` — каноны, которые лицензия покрывает. Fail-closed:
  канона нет в списке — не покрыт. Лицензия РФ перечисляет виды работ;
  сопоставить их с канонами — часть проверки человеком.
* ``licensed_locations`` — места салона из лицензии (опора §7A-3). Место
  чужого салона не добавляется: проверка на ``m2m_changed``, потому что
  M2M база сама не сверит.

Чего здесь нет, и почему
------------------------

Срока действия и отзыва: контракт молчит, юридическую семантику не
выдумываем. Отзыв или истечение после проверки база не видит — пробел
DRF-2839 (род DRF-2828); интерим — снять проверку вручную.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from .models import Tenant


class MedicalLicense(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: лицензия — запись с провенансом решения о допуске услуг;
    # удаление салона не должно молча стирать, на чём держался допуск.
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="medical_licenses")
    license_ref = models.CharField(max_length=100, help_text="Номер лицензии, как в документе.")

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", help_text="Кто проверил лицензию. Пусто — не проверена.",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_source_ref = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Основание проверки: где документ, что именно сверено.",
    )

    covered_templates = models.ManyToManyField(
        "services.ServiceTemplate", blank=True, related_name="medical_licenses",
        help_text="Каноны, которые лицензия покрывает. Нет в списке — не покрыт.",
    )
    licensed_locations = models.ManyToManyField(
        "tenants.ServiceLocation", blank=True, related_name="medical_licenses",
        help_text="Места этого салона, указанные в лицензии.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Медицинская лицензия"
        verbose_name_plural = "Медицинские лицензии"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "license_ref"], name="medicallicense_unique_ref_per_tenant",
            ),
            models.CheckConstraint(
                condition=~models.Q(license_ref=""), name="medicallicense_ref_required",
            ),
            # Проверка — решение человека: все три поля или ни одного.
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(verified_by__isnull=True)
                        & models.Q(verified_at__isnull=True)
                        & models.Q(verification_source_ref="")
                    )
                    | (
                        models.Q(verified_by__isnull=False)
                        & models.Q(verified_at__isnull=False)
                        & ~models.Q(verification_source_ref="")
                    )
                ),
                name="medicallicense_verification_all_or_nothing",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.license_ref} ({self.tenant_id})"

    @property
    def is_verified(self) -> bool:
        return self.verified_by_id is not None


@receiver(m2m_changed, sender=MedicalLicense.licensed_locations.through)
def _locations_belong_to_the_licensee(sender, instance, action, reverse, pk_set, **kwargs):
    """Место из лицензии — место ЭТОГО салона. M2M база не сверит, сверяем здесь."""
    if action != "pre_add" or not pk_set:
        return
    from .service_location import ServiceLocation

    if reverse:
        # instance — место, pk_set — лицензии.
        foreign = MedicalLicense.objects.filter(pk__in=pk_set).exclude(tenant_id=instance.tenant_id)
    else:
        foreign = ServiceLocation.objects.filter(pk__in=pk_set).exclude(tenant_id=instance.tenant_id)
    if foreign.exists():
        raise ValidationError(
            "Место лицензии должно принадлежать салону-лицензиату: место другого "
            "салона (или без салона) в лицензию не добавляется."
        )
