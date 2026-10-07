"""Состояние лицензии предложения — выводится, а не вводится (Body Care §7A-2).

Контракт v0.2 §3 кладёт ``tenant_medical_license_status``, ``license_ref``,
``license_service_scope``, ``licensed_address`` на предложение. Здесь они
**выводятся** из лицензии салона (``tenants.MedicalLicense``) и
подтверждённого класса канона (§7A-0): одна лицензия — много услуг, и
вводить её на каждую услугу значило бы размножить её по строкам.

### Состояния

``not_required``       класс подтверждён немедицинским;
``class_unconfirmed``  класс не подтверждён (``NULL``) или
                       ``legal_review_required`` — у любого канона и у
                       предложения без канона; гейт не открывает,
                       §7A-6 свернёт это в REVIEW_REQUIRED;
``not_verified``       класс медицинский, а проверенной лицензии у салона
                       нет — код ``MEDICAL_LICENSE_NOT_VERIFIED``;
``scope_mismatch``     проверенная лицензия есть, но канона в её объёме
                       нет — код ``LICENSE_SCOPE_MISMATCH``;
``verified``           проверенная лицензия салона покрывает канон.

### Что читается и что нет

Только **подтверждённый** класс ``ServiceTemplate.legal_service_class``
(§7A-0, ставит человек). Кандидат §7A-1 (``body_care_legal``) не читается
никогда: системный вывод гейт не открывает.

Лицензию требует и ``medical_cosmetology`` (буква §7A.3), и
``medical_other`` — **сверх буквы §7A.3, fail-closed, ждёт подтверждения
D-1** (главное окно, 06.10). Явно медицинский класс проверяется, даже если
у канона нет семейства body-care: «вне body-care» не отменяет
подтверждённого медицинского класса.

Класс проверяется независимо от области классификации канона
(``body_care_scope``): «вне Body Care» не значит «немедицинская». Правило
«неизвестный класс не снимает проверку» — под флагом
``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED`` (``body_care_scope.legal_checks_waived``);
при выключенном флаге канон без семейства и без класса по-прежнему
отвечает ``not_required``.

Адрес — §7A-3 (по месту мастера), свёртка в CAT-6 — §7A-6; здесь их нет.
Отзыв или истечение лицензии после проверки база не видит — DRF-2839.
"""

from __future__ import annotations

from collections.abc import Iterable

from services.body_care_scope import legal_checks_waived
from services.models import SalonService, ServiceTemplate
from tenants.models import MedicalLicense

NOT_REQUIRED = "not_required"
CLASS_UNCONFIRMED = "class_unconfirmed"
NOT_VERIFIED = "not_verified"
SCOPE_MISMATCH = "scope_mismatch"
VERIFIED = "verified"

LICENSE_STATES = (NOT_REQUIRED, CLASS_UNCONFIRMED, NOT_VERIFIED, SCOPE_MISMATCH, VERIFIED)

#: Коды §21 (бот-реестр) для отказных состояний.
CODES = {
    NOT_VERIFIED: "MEDICAL_LICENSE_NOT_VERIFIED",
    SCOPE_MISMATCH: "LICENSE_SCOPE_MISMATCH",
}

LC = ServiceTemplate.LegalServiceClass

#: Классы, которым нужна лицензия. ``medical_other`` — сверх буквы §7A.3.
LICENSE_REQUIRED_CLASSES = frozenset({LC.MEDICAL_COSMETOLOGY.value, LC.MEDICAL_OTHER.value})


def license_state_of(row: dict, verified_tenants: set, covered: set) -> str:
    """Состояние по строке предложения и покрытию из ``verified_coverage``.

    ``row`` — ключи ``tenant_id``, ``template_id``, ``template__service_family``,
    ``template__legal_service_class``. Общая точка для ``license_states`` и
    свёртки в состояние CAT-6 (§7A-6) — правило одно.
    """
    legal_class = row["template__legal_service_class"]
    if legal_class in LICENSE_REQUIRED_CLASSES:
        if row["tenant_id"] not in verified_tenants:
            return NOT_VERIFIED
        if (row["tenant_id"], row["template_id"]) not in covered:
            return SCOPE_MISMATCH
        return VERIFIED
    if legal_checks_waived(legal_class=legal_class, family=row["template__service_family"]):
        return NOT_REQUIRED
    return CLASS_UNCONFIRMED


def license_states(salon_service_ids: Iterable[object]) -> dict[object, str]:
    """``{pk предложения: состояние лицензии}`` — два запроса на пул.

    Неизвестный pk в ответ не попадает.
    """
    rows = list(
        SalonService.objects.filter(pk__in=list(salon_service_ids)).values(
            "pk",
            "tenant_id",
            "template_id",
            "template__service_family",
            "template__legal_service_class",
        )
    )
    verified_tenants, covered = verified_coverage({r["tenant_id"] for r in rows})
    return {r["pk"]: license_state_of(r, verified_tenants, covered) for r in rows}


def verified_coverage(tenant_ids) -> tuple[set, set]:
    """Один запрос: салоны с проверенной лицензией и пары (салон, канон) в её объёме."""
    verified_tenants: set = set()
    covered: set = set()
    for tenant_id, template_id in MedicalLicense.objects.filter(
        tenant_id__in=tenant_ids, verified_by__isnull=False
    ).values_list("tenant_id", "covered_templates"):
        verified_tenants.add(tenant_id)
        if template_id is not None:
            covered.add((tenant_id, template_id))
    return verified_tenants, covered


def license_state(salon_service: SalonService) -> str:
    """Состояние лицензии одного предложения."""
    return license_states([salon_service.pk])[salon_service.pk]
