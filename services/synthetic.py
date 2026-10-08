"""Пометка «синтетика»: тестовые данные, которые никогда не станут настоящими.

Решение владельца 08.10.2026 (сквозная проверка Плана на подготовленных
данных): механику продуктового пути проверяют на синтетических знаниях и
предложениях, обоснованность реальных рекомендаций — на подтверждённых.
Первое не доказывает второе, и одно не должно выглядеть как другое.

**Где пометка.** Поле ``synthetic`` — у знания (``ProcedureCapability``,
``CapabilityGoalLink``), у канона (``ServiceTemplate``) и у услуги салона
(``SalonService``). Предложение мастера своего поля не имеет: читается с
услуги салона. У салона и мастера своей пометки нет — тестовым салоном
служит существующий демо-салон (``Tenant.is_demo``), он уже скрыт от
обычного клиента во всех пулах.

**Почему синтетика не станет настоящей — замки базы (миграция 0052).**

* синтетическое знание не может иметь статус ``approved``;
* синтетическая услуга салона не может иметь ``mapping_status = verified``;
* пометка ставится при создании строки и не меняется никогда (триггер —
  ловит и ``QuerySet.update()``): иначе «снял пометку → подтвердил»
  обходило бы первые два замка;
* синтетическое привязывается только к синтетическому: способность и её
  канон, услуга салона и её канон, связь с целью и её способность несут
  одну и ту же пометку. Иначе синтетическая способность на настоящем
  каноне сделала бы под разрешением кандидатами настоящие предложения всех
  салонов, и тестовая запись ушла бы в настоящий слот;
* вся цепочка тестовая: синтетическая услуга не бывает без канона, живёт
  только в демо-салоне (и признак демо с такого салона не снять), а её
  предложение открывает только мастер этого же салона;
* ответ о проверке здоровья у синтетической строки подтверждает только
  названное синтетическое правило (:data:`SYNTHETIC_RULE`), человека нет;
  настоящей строке это правило запрещено.

**Кто читает синтетику — решает сервер, а не запрос.** Требование владельца:
«просьба вызывающего» — не поле в теле запроса. Поэтому право чтения — не
булево, а объект :class:`SyntheticGrant`, который выдаёт только
:func:`grant_for` и только по личности. Условий три, все серверные:

1. настройка ``SYNTHETIC_TEST_DATA_ENABLED`` — флаг стенда;
2. пользователь каталога входит в ``SYNTHETIC_TEST_SUBJECT_IDS``;
3. у пользователя стоит ``is_test_persona`` — без него демо-салон ему не
   показывает видимость (``users.sellable``), и синтетика «не работала бы»
   молча; так отказ стоит в одном месте.

Читатели принимают только этот объект. Голое ``True`` — ``TypeError``:
значение, приехавшее из тела запроса, разрешением стать не может по
построению. Разрешение перепроверяется при КАЖДОМ чтении по субъекту,
которого помнит сам объект: выключили флаг или убрали субъекта из списка —
выданное разрешение мертво, а передать его «за другого» нечем.

Обычный подбор (резолвер, полка) разрешения не выводит никогда.

**Допуск предложения** (``users.admission``): проверка MAPPING требует
``verified``, а синтетическая услуга ``verified`` быть не может. Под
разрешением допуск отвечает у MAPPING отдельным исходом
``CheckOutcome.SYNTHETIC`` (``reason=None``, ``catalog_answer`` — настоящий
``mapping_status`` строки) — только у синтетической строки С каноном и
статусом «связана, не подтверждена» (``review_required``); иначе обычный
отказ. Не ``PASSED`` и не ``NOT_ENFORCED``: вердикт по предложению открыт, а
вызывающий обязан прочесть исход. Исключение — только для подтверждения
связи; остальные проверки для синтетической строки не ослабляются.
Разрешение допуск выводит из своего ``viewer`` (``grant_for(viewer)``).

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

_ISSUER = object()


class SyntheticGrant:
    """Право читать синтетику. Выдаёт только :func:`grant_for`.

    Помнит субъекта и больше ничего: само право каждый раз перепроверяется
    по настройкам (:func:`reads_synthetic`). Собрать объект в обход
    :func:`grant_for` — ``TypeError``.
    """

    __slots__ = ("_subject_id",)

    def __init__(self, subject_id: str, *, _issued_by: object = None) -> None:
        if _issued_by is not _ISSUER:
            raise TypeError("SyntheticGrant выдаёт только services.synthetic.grant_for(user)")
        object.__setattr__(self, "_subject_id", subject_id)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("SyntheticGrant неизменяем")

    @property
    def subject_id(self) -> str:
        return self._subject_id

    def __repr__(self) -> str:
        return f"SyntheticGrant(subject_id={self._subject_id!r})"


def synthetic_data_enabled() -> bool:
    """Флаг стенда. Читается при каждом вызове, не кешируется."""
    return bool(getattr(settings, "SYNTHETIC_TEST_DATA_ENABLED", False))


def _subject_is_listed(subject_id: str) -> bool:
    return subject_id in {str(item) for item in getattr(settings, "SYNTHETIC_TEST_SUBJECT_IDS", ())}


def grant_for(user) -> SyntheticGrant | None:
    """Разрешение для этого пользователя каталога — или ``None``.

    ``None`` и анонимный пользователь — ``None`` без запроса и без
    исключения. Условия — в докстринге модуля; все читаются с уже
    загруженного объекта и из настроек.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if not synthetic_data_enabled() or not getattr(user, "is_test_persona", False):
        return None
    subject_id = str(user.pk)
    if not _subject_is_listed(subject_id):
        return None
    return SyntheticGrant(subject_id, _issued_by=_ISSUER)


def reads_synthetic(grant: SyntheticGrant | None) -> bool:
    """Разрешение предъявлено и ещё действует.

    Принимает только :class:`SyntheticGrant` или ``None``. Любое другое
    значение — ``TypeError``, в том числе ``True``: булево из тела запроса
    правом не становится.
    """
    if grant is None:
        return False
    if not isinstance(grant, SyntheticGrant):
        raise TypeError(
            "include_synthetic принимает только SyntheticGrant из services.synthetic.grant_for(user) "
            f"или None, получено {type(grant).__name__}"
        )
    return synthetic_data_enabled() and _subject_is_listed(grant.subject_id)


def knowledge_q(now: datetime, prefix: str = "", *, include_synthetic: SyntheticGrant | None = None) -> Q:
    """Знание, которое можно читать: подтверждённое ИЛИ помеченное под разрешением.

    Область (``supported``) и срок годности действуют на ОБЕ половины:
    запрещённое и истёкшее не читается и синтетикой.

    ``prefix`` — путь до строки с основанием (``"capability__"`` у связи).
    Без разрешения возвращает прежнее условие: синтетическая строка не может
    быть ``approved``, поэтому в подтверждённую половину не попадает.
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


def offer_reads_as_synthetic(salon_service, *, include_synthetic: SyntheticGrant | None) -> bool:
    """Услуга помечена синтетикой И разрешение действует."""
    return reads_synthetic(include_synthetic) and bool(getattr(salon_service, "synthetic", False))


def offers_reading_as_synthetic(
    salon_service_ids: Iterable, *, include_synthetic: SyntheticGrant | None,
) -> frozenset:
    """То же пачкой по id — одним запросом; без разрешения запроса нет вовсе."""
    if not reads_synthetic(include_synthetic):
        return frozenset()
    from .models import SalonService

    return frozenset(
        SalonService.objects.filter(pk__in=list(salon_service_ids), synthetic=True).values_list("pk", flat=True)
    )
