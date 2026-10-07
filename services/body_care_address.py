"""Адрес медицинской услуги — место мастера в лицензии салона (Body Care §7A-3).

Контракт v0.2 §7A.5: медицинское предложение оказывается по адресу из
лицензии; несовпадение — ``LICENSE_ADDRESS_MISMATCH`` (F-BC-017). Где
оказывается услуга — там, где работает мастер (``SpecialistProfile.works_at``,
§9). Поэтому ответ — по паре ``(specialist_id, salon_service_id)``: одну
услугу салона делают разные мастера в разных местах.

### Состояния (решения главного окна, 06.10)

``not_required``         класс канона подтверждён немедицинским, либо у
                         канона нет ни семейства, ни класса;
``class_unconfirmed``    класс не подтверждён / ``legal_review_required``;
``location_unknown``     места у мастера нет, или оно не подтверждено
                         (§9: неподтверждённое место — «происхождение
                         неизвестно»); fail-closed, гейт не открывает;
``no_covering_license``  у салона нет проверенной лицензии, покрывающей
                         канон, — адрес сверять не с чем; точный код даёт
                         гейт лицензии §7A-2 (``MEDICAL_LICENSE_NOT_VERIFIED``
                         / ``LICENSE_SCOPE_MISMATCH``), а не этот;
``address_mismatch``     покрывающая лицензия есть, а подтверждённого места
                         мастера в ней нет — код ``LICENSE_ADDRESS_MISMATCH``;
``verified``             место мастера подтверждено и указано в проверенной
                         лицензии того же салона, покрывающей канон.

Читаются только подтверждённые поля §7A-0 (классы — как в §7A-2), кандидат
§7A-1 — никогда. Исключение кандидата — дело CAT-10-ext и гейта записи.
"""

from __future__ import annotations

from collections.abc import Iterable

from services.body_care_license import LICENSE_REQUIRED_CLASSES
from services.body_care_scope import legal_checks_waived
from services.models import SalonService
from tenants.models import LocationStatus, MedicalLicense
from users.models import SpecialistProfile

NOT_REQUIRED = "not_required"
CLASS_UNCONFIRMED = "class_unconfirmed"
LOCATION_UNKNOWN = "location_unknown"
NO_COVERING_LICENSE = "no_covering_license"
ADDRESS_MISMATCH = "address_mismatch"
VERIFIED = "verified"

ADDRESS_STATES = (
    NOT_REQUIRED, CLASS_UNCONFIRMED, LOCATION_UNKNOWN, NO_COVERING_LICENSE, ADDRESS_MISMATCH, VERIFIED,
)

#: Код §21 (бот-реестр) для отказного состояния этого гейта.
CODES = {ADDRESS_MISMATCH: "LICENSE_ADDRESS_MISMATCH"}


def _state(offering: dict, master: dict, covering: set, licensed: set) -> str:
    legal_class = offering["template__legal_service_class"]
    if legal_class not in LICENSE_REQUIRED_CLASSES:
        # То же правило, что у лицензии (§7A-2): снимает проверку только
        # подтверждённый немедицинский класс.
        if legal_checks_waived(legal_class=legal_class, family=offering["template__service_family"]):
            return NOT_REQUIRED
        return CLASS_UNCONFIRMED
    place = master["works_at_id"]
    if place is None or master["works_at__status"] != LocationStatus.CONFIRMED:
        return LOCATION_UNKNOWN
    key = (offering["tenant_id"], offering["template_id"])
    if key not in covering:
        return NO_COVERING_LICENSE
    if (*key, place) not in licensed:
        return ADDRESS_MISMATCH
    return VERIFIED


def address_states(pairs: Iterable[tuple[object, object]]) -> dict[tuple[object, object], str]:
    """``{(specialist_id, salon_service_id): состояние}`` — три запроса на пул.

    Пара с неизвестным мастером или предложением в ответ не попадает.
    """
    pairs = list(pairs)
    offerings = {
        o["pk"]: o
        for o in SalonService.objects.filter(pk__in={ss for _, ss in pairs}).values(
            "pk", "tenant_id", "template_id", "template__service_family", "template__legal_service_class"
        )
    }
    masters = {
        m["pk"]: m
        for m in SpecialistProfile.objects.filter(pk__in={sp for sp, _ in pairs}).values(
            "pk", "works_at_id", "works_at__status"
        )
    }
    covering: set = set()
    licensed: set = set()
    for tenant_id, template_id, location_id in MedicalLicense.objects.filter(
        tenant_id__in={o["tenant_id"] for o in offerings.values()},
        verified_by__isnull=False,
        covered_templates__isnull=False,
    ).values_list("tenant_id", "covered_templates", "licensed_locations"):
        covering.add((tenant_id, template_id))
        if location_id is not None:
            licensed.add((tenant_id, template_id, location_id))
    return {
        (sp, ss): _state(offerings[ss], masters[sp], covering, licensed)
        for sp, ss in pairs
        if ss in offerings and sp in masters
    }


def address_state(specialist_id, salon_service_id) -> str:
    """Состояние одной пары."""
    return address_states([(specialist_id, salon_service_id)])[(specialist_id, salon_service_id)]
