"""Единая точка ЧТЕНИЯ каталога: легаси ``Service`` + канонический слой.

Зачем модуль существует
-----------------------
Каталог живёт в двух слоях одновременно (незавершённая strangler-fig
миграция, чанк S3-CUT — см. ``services/models.py``):

* легаси ``Service`` — маркетплейсная модель, её наполняет Pro-приложение;
* канонический ``ServiceTemplate -> SalonService -> SpecialistService`` —
  его наполняет приёмка салонов (интейк / YClients / seed).

Замер боевого пилота 2026-08-30 (прямой запрос к ``dev-web-1``)::

    SpecialistService   292
    SalonService         94
    Service (легаси)      0

Легаси-таблица пуста ЦЕЛИКОМ. Поэтому всякая поверхность, читавшая
только ``Service``, молча отдавала пустоту — и выглядело это как
«ничего не нашлось», а не как поломка. Здесь собраны читающие примитивы,
чтобы поверхности не расходились в трактовке двух слоёв.

Объединение, а не замена
------------------------
Читаем ОБА слоя. Легаси не мёртв по схеме: маркетплейсный тенант,
работающий через Pro-приложение, пишет ровно в него, и
``services.service_resolver.resolve_bookable_service`` до сих пор отдаёт
ему приоритет при коллизии UUID. Переключить чтение «с легаси на канон»
значило бы погасить работающий сегодня маркетплейс.

Категория: шаблон — ЗАПАСНОЙ путь, а не объединение
---------------------------------------------------
``SalonService.category`` обнуляем по схеме (обязателен только когда нет
``template``), а ``ServiceTemplate.category`` — NOT NULL. Услуга,
заведённая от шаблона без своей категории, без запасного пути выпадала бы
из категорийных поверхностей молча.

Но это именно фолбэк: услуга, которой салон ЯВНО назначил категорию,
шаблоном не переопределяется — иначе мы приписываем салону то, чего он не
объявлял (DRF-1308 п.1 и п.4). В ORM это ровно ``Coalesce`` — второй
аргумент читается только когда первый NULL. Симметрично
``RecommendationEngine._goal_category_predicate``.

Какой ``id`` отдаём наружу
--------------------------
Тот, который принимает бронирование. Для канонического слоя это
``SalonService.id``: его разбирает ``resolve_bookable_service`` и он же
ложится в ``Appointment.salon_service``. ``SpecialistService.id`` —
внутренний ключ зеркала каталога для бота, поверхностям записи он не
адресован.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, Q, QuerySet
from django.db.models.functions import Coalesce

# --------------------------------------------------------------------------- #
# Разрешение категории (фолбэк на шаблон)
# --------------------------------------------------------------------------- #

# Путь до разрешённой категории от ``SpecialistProfile``. Собран строкой,
# чтобы одинаково работать и в ``filter()``, и в ``annotate()``.
_CANON = "specialist_services__salon_service"


def resolved_category_id_expression(prefix: str = ""):
    """``Coalesce`` разрешённой категории: своя, иначе — шаблона.

    ``prefix`` — путь до ``SalonService`` от модели, по которой строится
    запрос (например ``"salon_service__"`` от ``SpecialistService``).
    """
    return Coalesce(
        f"{prefix}category_id", f"{prefix}template__category_id",
    )


def resolved_category(salon_service):
    """Категория услуги салона: своя, иначе — категория шаблона.

    Питоновский близнец ``resolved_category_id_expression`` для случаев,
    когда объект уже загружен.
    """
    if salon_service.category_id is not None:
        return salon_service.category
    template = salon_service.template
    return template.category if template is not None else None


def _canonical_category_text_q(field: str, needle: str) -> Q:
    """Совпадение по тексту категории с запасным путём через шаблон.

    ``field`` — ``"slug"`` или ``"name"``. Ветка шаблона доступна ТОЛЬКО
    когда своей категории нет вовсе (``category IS NULL``).
    """
    return (
        Q(**{f"{_CANON}__category__{field}__icontains": needle})
        | Q(
            **{
                f"{_CANON}__category__isnull": True,
                f"{_CANON}__template__category__{field}__icontains": needle,
            }
        )
    )


# --------------------------------------------------------------------------- #
# Текстовое совпадение услуги — предикат над ``SpecialistProfile``
# --------------------------------------------------------------------------- #


def specialist_service_text_q(needle: str) -> Q:
    """Мастер предлагает услугу, совпадающую с ``needle`` по тексту.

    ILIKE по названию услуги и по slug/названию её категории — в ОБОИХ
    слоях каталога. Форма предиката та же, что была у легаси-варианта,
    чтобы наблюдаемый контракт поиска не поехал.
    """
    legacy = Q(services__is_active=True) & (
        Q(services__name__icontains=needle)
        | Q(services__category__slug__icontains=needle)
        | Q(services__category__name__icontains=needle)
    )
    canonical = Q(
        specialist_services__is_active=True,
        specialist_services__salon_service__is_active=True,
    ) & (
        Q(**{f"{_CANON}__name__icontains": needle})
        | _canonical_category_text_q("slug", needle)
        | _canonical_category_text_q("name", needle)
    )
    return legacy | canonical


# --------------------------------------------------------------------------- #
# Подсчёты по категориям
# --------------------------------------------------------------------------- #


def category_service_counts(
    *, specialist_ids=None,
) -> dict[uuid.UUID, int]:
    """Сколько активных услуг в каждой категории (оба слоя каталога).

    ``specialist_ids`` — ограничение пула мастеров; ``None`` = все.
    Возвращает ``{category_id: count}``; категории без услуг отсутствуют.
    """
    from services.models import Service, SpecialistService

    legacy = Service.objects.filter(is_active=True, category__isnull=False)
    canonical = SpecialistService.objects.filter(
        is_active=True, salon_service__is_active=True,
    )
    if specialist_ids is not None:
        legacy = legacy.filter(specialist_id__in=specialist_ids)
        canonical = canonical.filter(specialist_id__in=specialist_ids)

    counts: dict[uuid.UUID, int] = {}
    for row in legacy.values("category_id").annotate(n=Count("id", distinct=True)):
        counts[row["category_id"]] = counts.get(row["category_id"], 0) + row["n"]

    canonical_rows = (
        canonical
        .annotate(resolved_category=resolved_category_id_expression("salon_service__"))
        .filter(resolved_category__isnull=False)
        .values("resolved_category")
        .annotate(n=Count("id", distinct=True))
    )
    for row in canonical_rows:
        key = row["resolved_category"]
        counts[key] = counts.get(key, 0) + row["n"]
    return counts


def category_specialist_counts() -> dict[uuid.UUID, int]:
    """Сколько РАЗНЫХ мастеров держат активную услугу в каждой категории.

    Отличается от :func:`category_service_counts` единицей счёта: там
    услуги, здесь мастера. Один мастер с тремя услугами в категории — это
    один мастер.

    Дедупликация делается в питоне, а не двумя ``COUNT(DISTINCT)``:
    сложить два счётчика нельзя — мастер, у которого есть услуги в ОБОИХ
    слоях каталога, посчитался бы дважды. Цена — пара
    ``(category_id, specialist_id)`` на активную услугу; на пилоте это
    294 пары, а единственный вызывающий кэширует результат на час.
    """
    from services.models import Service, SpecialistService

    per_category: dict[uuid.UUID, set[uuid.UUID]] = {}

    legacy = (
        Service.objects
        .filter(is_active=True, category__isnull=False)
        .values_list("category_id", "specialist_id")
    )
    for category_id, specialist_id in legacy:
        per_category.setdefault(category_id, set()).add(specialist_id)

    canonical = (
        SpecialistService.objects
        .filter(is_active=True, salon_service__is_active=True)
        .annotate(resolved_category=resolved_category_id_expression("salon_service__"))
        .filter(resolved_category__isnull=False)
        .values_list("resolved_category", "specialist_id")
    )
    for category_id, specialist_id in canonical:
        per_category.setdefault(category_id, set()).add(specialist_id)

    return {key: len(value) for key, value in per_category.items()}


# --------------------------------------------------------------------------- #
# Список услуг мастера
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CatalogService:
    """Услуга мастера, одинаковая по форме для обоих слоёв каталога.

    ``id`` — ключ, который принимает бронирование этой услуги (см.
    докстринг модуля). ``source`` оставлен наружу намеренно: потребитель
    должен уметь отличить маркетплейсную строку от салонной, не гадая по
    форме id.
    """

    id: uuid.UUID
    source: str  # "marketplace" | "salon"
    name: str
    description: str
    price: Decimal
    duration_minutes: int | None
    category_id: uuid.UUID | None
    category_name: str | None
    category_slug: str | None
    sort_order: int
    image_url: str | None


def _legacy_rows(specialist) -> list[CatalogService]:
    rows = specialist.services.all()
    out: list[CatalogService] = []
    for service in rows:
        if not service.is_active:
            continue
        category = service.category
        out.append(CatalogService(
            id=service.id,
            source="marketplace",
            name=service.name,
            description=service.description,
            price=service.price,
            duration_minutes=service.duration_minutes,
            category_id=service.category_id,
            category_name=category.name if category else None,
            category_slug=category.slug if category else None,
            sort_order=service.sort_order,
            image_url=service.image.url if service.image else None,
        ))
    out.sort(key=lambda row: (row.sort_order, row.name))
    return out


def _canonical_rows(specialist) -> list[CatalogService]:
    out: list[CatalogService] = []
    for link in specialist.specialist_services.all():
        salon = link.salon_service
        if not link.is_active or not salon.is_active:
            continue
        category = resolved_category(salon)
        out.append(CatalogService(
            id=salon.id,
            source="salon",
            name=salon.name,
            # ``SalonService`` описания не хранит — пустая строка честнее
            # выдуманного текста.
            description="",
            price=link.price,
            duration_minutes=link.resolved_duration(),
            category_id=category.id if category else None,
            category_name=category.name if category else None,
            category_slug=category.slug if category else None,
            # Порядка в каноническом слое нет по схеме — сортируем по имени.
            sort_order=0,
            image_url=None,
        ))
    out.sort(key=lambda row: row.name)
    return out


def catalog_services_for(specialist) -> list[CatalogService]:
    """Активные услуги мастера из ОБОИХ слоёв каталога.

    Сначала маркетплейсные (в их собственном ``sort_order``), затем
    салонные по алфавиту. Порядок стабилен между репликами: внутри
    каждого слоя ключ сортировки полный.

    Фильтрация по ``is_active`` делается в питоне, а не в запросе, чтобы
    не ронять ``prefetch_related`` вызывающей стороны в N+1.
    """
    return _legacy_rows(specialist) + _canonical_rows(specialist)


def catalog_services_prefetch() -> tuple:
    """``prefetch_related``-аргументы для :func:`catalog_services_for`.

    Два ``Prefetch`` вместо семи строковых путей: прямые FK внутри
    каждого слоя забираются одним ``select_related``, то есть два запроса
    на страницу вместо семи.
    """
    from django.db.models import Prefetch

    from services.models import Service, SpecialistService

    return (
        Prefetch(
            "services",
            queryset=Service.objects.select_related("category"),
        ),
        Prefetch(
            "specialist_services",
            queryset=SpecialistService.objects.select_related(
                "salon_service",
                "salon_service__category",
                "salon_service__template",
                "salon_service__template__category",
            ),
        ),
    )


def annotate_catalog_services_count(qs: QuerySet) -> QuerySet:
    """Аннотирует ``active_services_count`` по обоим слоям каталога.

    Две отдельные аннотации со своими ``filter=`` — иначе Django склеит
    оба JOIN в одно ``COUNT`` и перемножит строки.
    """
    return qs.annotate(
        _legacy_services_count=Count(
            "services", filter=Q(services__is_active=True), distinct=True,
        ),
        _canonical_services_count=Count(
            "specialist_services",
            filter=Q(
                specialist_services__is_active=True,
                specialist_services__salon_service__is_active=True,
            ),
            distinct=True,
        ),
    ).annotate(
        active_services_count=(
            Coalesce("_legacy_services_count", 0)
            + Coalesce("_canonical_services_count", 0)
        ),
    )
