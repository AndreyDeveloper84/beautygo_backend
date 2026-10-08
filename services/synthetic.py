"""Пометка «синтетика»: тестовые данные, которые никогда не станут настоящими.

Решение владельца 08.10.2026 (сквозная проверка Плана на подготовленных
данных): механику продуктового пути проверяют на синтетических знаниях и
предложениях, обоснованность реальных рекомендаций — на подтверждённых.
Первое не доказывает второе, и одно не должно выглядеть как другое.

**Где пометка.** Поле ``synthetic`` — у знания (``ProcedureCapability``,
``CapabilityGoalLink``), у канона (``ServiceTemplate``) и у услуги салона
(``SalonService``). Предложение мастера своего поля не имеет: читается с
услуги салона.

**Почему синтетика не станет настоящей — замки базы (миграция 0052).**

* синтетическое знание не может иметь статус ``approved``;
* синтетическая услуга салона не может иметь ``mapping_status = verified``;
* пометка ставится при создании строки и не меняется никогда (триггер —
  ловит и ``QuerySet.update()``): иначе «снял пометку → подтвердил»
  обходило бы первые два замка;
* синтетическое привязывается только к синтетическому: способность и её
  канон, услуга салона и её канон, связь с целью и её способность несут
  одну и ту же пометку. Иначе синтетическая способность на настоящем
  каноне сделала бы под флагом кандидатами настоящие предложения всех
  салонов, и тестовая запись ушла бы в настоящий слот;
* ответ о проверке здоровья у синтетической строки подтверждает только
  названное синтетическое правило (:data:`SYNTHETIC_RULE`), человека нет;
  настоящей строке это правило запрещено.

**Два фактора чтения — оба обязательны.**

1. настройка ``SYNTHETIC_TEST_DATA_ENABLED`` (флаг стенда, по умолчанию
   выключена) — :func:`synthetic_data_enabled`;
2. явный запрос вызывающего — ``include_synthetic=True``.

Нет любого из двух — синтетики не существует ни для одного читателя.
Обычный подбор (резолвер, полка) второй фактор не передаёт никогда.

**Допуск предложения** (``users.admission``): проверка MAPPING требует
``verified``, а синтетическая услуга ``verified`` быть не может. Под двумя
факторами допуск отвечает у MAPPING отдельным исходом
``CheckOutcome.SYNTHETIC`` (``reason=None``, ``catalog_answer`` — настоящий
``mapping_status`` строки): не ``PASSED`` и не ``NOT_ENFORCED``. Вердикт по
предложению при этом открыт, а вызывающий обязан прочесть исход — синтетика
нигде не выглядит как подтверждённое. Остальные проверки для синтетической
строки не ослабляются.

Сам этот модуль ничего не помечает: пометка ставится только при создании
строки кодом сида.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from django.conf import settings
from django.db.models import Q

#: Единственное основание, которым подтверждается ответ о проверке здоровья у
#: синтетической строки. Настоящей строке запрещено ограничением базы.
SYNTHETIC_RULE = "synthetic-test-data"


def synthetic_data_enabled() -> bool:
    """Первый фактор: флаг стенда. Читается при каждом вызове, не кешируется."""
    return bool(getattr(settings, "SYNTHETIC_TEST_DATA_ENABLED", False))


def reads_synthetic(include_synthetic: bool) -> bool:
    """Оба фактора: вызывающий попросил И стенд разрешил."""
    return bool(include_synthetic) and synthetic_data_enabled()


def knowledge_q(now: datetime, prefix: str = "", *, include_synthetic: bool = False) -> Q:
    """Знание, которое можно читать: подтверждённое ИЛИ помеченное под двумя факторами.

    Область (``supported``) и срок годности действуют на ОБЕ половины:
    запрещённое и истёкшее не читается и синтетикой.

    ``prefix`` — путь до строки с основанием (``"capability__"`` у связи).
    Без факторов возвращает ровно прежнее условие: синтетическая строка не
    может быть ``approved``, поэтому в подтверждённую половину не попадает.
    """
    from .models import ClaimEvidence

    admitted = Q(**{f"{prefix}status": ClaimEvidence.Status.APPROVED}) & Q(**{f"{prefix}synthetic": False})
    if reads_synthetic(include_synthetic):
        admitted = admitted | Q(**{f"{prefix}synthetic": True})
    return (
        admitted
        & Q(**{f"{prefix}claim_scope": ClaimEvidence.ClaimScope.SUPPORTED})
        & (Q(**{f"{prefix}valid_until__isnull": True}) | Q(**{f"{prefix}valid_until__gt": now}))
    )


def real_offer_q(prefix: str = "") -> Q:
    """Услуга салона настоящая. ``prefix`` — путь до ``SalonService``."""
    return Q(**{f"{prefix}synthetic": False})


def real_template_q(prefix: str = "") -> Q:
    """Канон настоящий. ``prefix`` — путь до ``ServiceTemplate``."""
    return Q(**{f"{prefix}synthetic": False})


def offer_reads_as_synthetic(salon_service, *, include_synthetic: bool) -> bool:
    """Услуга помечена синтетикой И оба фактора на месте."""
    return bool(getattr(salon_service, "synthetic", False)) and reads_synthetic(include_synthetic)


def offers_reading_as_synthetic(salon_service_ids: Iterable, *, include_synthetic: bool) -> frozenset:
    """То же пачкой по id — одним запросом; без факторов запроса нет вовсе."""
    if not reads_synthetic(include_synthetic):
        return frozenset()
    from .models import SalonService

    return frozenset(
        SalonService.objects.filter(pk__in=list(salon_service_ids), synthetic=True).values_list("pk", flat=True)
    )
