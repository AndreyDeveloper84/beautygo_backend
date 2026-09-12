"""Статус связи услуги с шаблоном и его происхождение — §76, T14/T15.

Предмет набора — не «поле добавлено», а **невозможность объявить связь
проверенной, не сказав, чем это доказано**. Разница принципиальная:
поле со статусом без provenance повторило бы историю литерала рейтинга
дословно — значение, поставленное скриптом, читается позже как
проверенный факт.

Поэтому проверка стоит **в схеме**, а не в ``clean()``: ``clean()``
обходится любым ``update()`` и любой миграцией данных, то есть ровно
теми путями, которыми статус и будет проставляться массово.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

S = SalonService.MappingStatus


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="mapstatus-tenant", name="Mapping Status Salon")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Маникюр", slug="mapstatus-manicure")


@pytest.fixture
def human(db):
    return User.objects.create_user(username="mapstatus-human", password="x")


def _service(tenant, category, name="Услуга", **overrides):
    fields = dict(
        tenant=tenant, category=category, name=name,
        duration_minutes=60, base_price=Decimal("1500"),
    )
    fields.update(overrides)
    return SalonService.objects.create(**fields)


def _template(category, name="Канон для VERIFIED"):
    """DRF-1668: `VERIFIED` ⇒ `template` (схема) — подтверждение это связь
    С ЧЕМ-ТО; строки, проверяющие провенанс `VERIFIED`, несут шаблон."""
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:20], duration_default=60,
    )


# ---------------------------------------------------------------------------
# Умолчание
# ---------------------------------------------------------------------------


def test_new_service_is_unmapped_not_review_required(tenant, category):
    """Умолчание — `UNMAPPED`, и это не придирка к слову.

    Строка, про которую ещё ничего не сказано, не должна выглядеть как
    строка, про которую сказано «связь есть». `REVIEW_REQUIRED`
    умолчанием означал бы, что каждая новая услуга сама себя объявила
    кандидатом на проверку — то есть статус начал бы утверждать то,
    чего никто не утверждал.
    """
    assert _service(tenant, category).mapping_status == S.UNMAPPED


# ---------------------------------------------------------------------------
# VERIFIED невозможен без provenance — §76
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provenance, missing",
    [
        pytest.param({}, "всё", id="ничего"),
        pytest.param(
            {"mapping_confirmed_at": "now"}, "кто/правило и основание", id="только-когда",
        ),
        pytest.param(
            {"mapping_confirmed_at": "now", "mapping_source_ref": "draft:1"},
            "кто или какое правило",
            id="когда-и-основание-без-кто",
        ),
        pytest.param(
            {"mapping_confirmed_by": "human", "mapping_source_ref": "draft:1"},
            "когда",
            id="кто-и-основание-без-когда",
        ),
        pytest.param(
            {"mapping_confirmed_by": "human", "mapping_confirmed_at": "now"},
            "ссылка на основание",
            id="кто-и-когда-без-основания",
        ),
    ],
)
def test_verified_without_full_provenance_is_rejected_by_the_schema(
    tenant, category, human, provenance, missing,
):
    """§76: статус хранит, КТО или какое правило, КОГДА и ПО КАКОМУ основанию.

    Каждый случай снимает ровно одну часть — иначе тест доказывал бы,
    что «совсем пустое не проходит», и молчал бы про частичное
    происхождение, которое и есть настоящий способ соврать: заполнить
    два поля из трёх выглядит добросовестно.
    """
    resolved = {
        key: (timezone.now() if value == "now" else human if value == "human" else value)
        for key, value in provenance.items()
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        _service(tenant, category, mapping_status=S.VERIFIED, **resolved)


def test_verified_with_human_provenance_is_allowed(tenant, category, human):
    """Положительная стража: правило не выродилось в «VERIFIED нельзя никогда».

    Без неё все проверки выше зеленели бы и на схеме, которая просто
    запрещает статус, — а тогда шкала владельца была бы двухзначной,
    и «подтверждено» стало бы недостижимым состоянием.
    """
    service = _service(
        tenant, category, name="Подтверждена человеком",
        template=_template(category),
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="draft:0c2f",
    )
    assert service.mapping_status == S.VERIFIED


def test_rule_confirmation_without_a_version_is_rejected(tenant, category):
    """«Подтверждено правилом» без версии — «подтверждено какой-то из версий».

    Правило меняется, и связь, законная по вчерашней редакции, может
    быть незаконной по сегодняшней. Без версии отличить одно от другого
    нельзя уже никогда — запись останется, а её основание испарится.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        _service(
            tenant, category, name="Правило без версии",
            mapping_status=S.VERIFIED,
            mapping_confirmed_rule="canonical_exact_name",
            mapping_confirmed_at=timezone.now(),
            mapping_source_ref="rule-run:2026-09-08",
        )


def test_rule_confirmation_with_a_version_is_allowed(tenant, category):
    """Вторая половина определения владельца: не только человек подтверждает.

    `VERIFIED` даёт и **детерминированное правило с зафиксированным
    provenance** — иначе разметка каталога была бы обречена быть ручной
    целиком, а владелец этого не говорил.
    """
    service = _service(
        tenant, category, name="Правило с версией",
        template=_template(category, "Канон для правила"),
        mapping_status=S.VERIFIED,
        mapping_confirmed_rule="canonical_exact_name",
        mapping_rule_version="1.0.0",
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="rule-run:2026-09-08",
    )
    assert service.mapping_status == S.VERIFIED


@pytest.mark.parametrize("status", [S.UNMAPPED, S.REVIEW_REQUIRED])
def test_unverified_statuses_need_no_provenance(tenant, category, status):
    """Требование provenance касается только утверждения, а не его отсутствия.

    `REVIEW_REQUIRED` — это признание «мы не знаем», и требовать
    доказательства незнания значило бы сделать миграцию невозможной:
    ей ровно это и предстоит проставить 206 строкам.
    """
    service = _service(tenant, category, name=f"Без provenance {status}", mapping_status=status)
    assert service.mapping_status == status


# ---------------------------------------------------------------------------
# Миграция — T15. Проверяется поведение, а не факт запуска
# ---------------------------------------------------------------------------


def test_link_to_template_becomes_review_required_never_verified(tenant, category):
    """206 связей пилота уходят в `REVIEW_REQUIRED`, а не в `VERIFIED`.

    Правило владельца: **сам факт наличия связи не доказывает её
    правильность**. На пилоте это подтверждается данными буквально —
    все 206 связей поставлены `seed_demo_salons`, тем же файлом, который
    пишет литерал рейтинга; ни у одного из 58 драфтов не заполнен
    `confirmed_by`, то есть подтверждение состоялось, а подтвердившего
    нет (замер 08.09, у него срок годности).
    """
    from services.migrations import _mapping_status_backfill as backfill

    template = ServiceTemplate.objects.create(
        category=category, name="Классический маникюр",
        name_short="Маникюр", duration_default=60,
    )
    linked = _service(tenant, category, name="Со связью", template=template)
    orphan = _service(tenant, category, name="Без связи")

    backfill.classify(SalonService)

    linked.refresh_from_db()
    orphan.refresh_from_db()
    assert linked.mapping_status == S.REVIEW_REQUIRED
    assert orphan.mapping_status == S.UNMAPPED
    assert not SalonService.objects.filter(mapping_status=S.VERIFIED).exists()


def test_backfill_does_not_touch_a_status_already_decided(tenant, category, human):
    """Миграция не переписывает решение, которое уже кто-то принял.

    Иначе повторный прогон backfill сбросил бы подтверждённую связь
    в `REVIEW_REQUIRED` — то есть уничтожил бы человеческую работу
    тем самым механизмом, который заведён, чтобы её сохранить.
    """
    from services.migrations import _mapping_status_backfill as backfill

    template = ServiceTemplate.objects.create(
        category=category, name="Стоун-массаж", name_short="Стоун", duration_default=60,
    )
    confirmed = _service(
        tenant, category, name="Уже подтверждена", template=template,
        mapping_status=S.VERIFIED,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="draft:0c2f",
    )

    backfill.classify(SalonService)

    confirmed.refresh_from_db()
    assert confirmed.mapping_status == S.VERIFIED


# ---------------------------------------------------------------------------
# Третий исход §93: отказ — это решение, а не отсутствие
# ---------------------------------------------------------------------------


def test_refusal_is_not_the_default(tenant, category):
    """Новая строка — `UNMAPPED`, а не отказ.

    Если бы отказ был умолчанием, каждая заведённая услуга объявляла бы
    сама о себе «рекомендовать не будем» — то есть решение принималось
    бы кодом за человека, ровно как §90 запретил делать с признаком
    здоровья.
    """
    assert _service(tenant, category).mapping_status != S.NOT_RECOMMENDABLE


def test_refusal_without_provenance_is_refused_by_the_database(tenant, category):
    """Отказ без автора и основания невозможен на уровне схемы.

    Отказ — решение человека, и у решения обязан быть автор, дата и
    основание. Иначе через месяц строка со статусом «не подлежит»
    неотличима от строки, которую так проставил чей-то `update()`.

    Проверка в базе, а не в `clean()`, по той же причине, что и у
    `verified`: `clean()` обходится любым `update()`.
    """
    with pytest.raises(IntegrityError, match="not_recommendable_requires_provenance"):
        with transaction.atomic():
            _service(
                tenant, category, name="Отказ без автора",
                mapping_status=S.NOT_RECOMMENDABLE,
            )


def test_refusal_with_provenance_is_accepted(tenant, category, human):
    """Положительная стража: правильно оформленный отказ записывается.

    Без неё проверка выше зеленела бы и на схеме, которая запрещает
    четвёртое состояние вовсе, — а тогда разбор 56 услуг снова было бы
    некуда записывать.
    """
    before = SalonService.objects.filter(mapping_status=S.NOT_RECOMMENDABLE).count()
    service = _service(
        tenant, category, name="Продажа подарочного сертификата",
        mapping_status=S.NOT_RECOMMENDABLE,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор 56 услуг, строка 41: это не процедура",
    )
    assert service.mapping_status == S.NOT_RECOMMENDABLE
    assert (
        SalonService.objects.filter(mapping_status=S.NOT_RECOMMENDABLE).count()
        == before + 1
    )


def test_refusal_and_unmapped_are_distinct_rows_in_the_database(tenant, category, human):
    """Отказ и отсутствие различимы выборкой, а не только на словах.

    Это то, ради чего состояние заведено. Очередь разбора выбирается
    как `mapping_status=UNMAPPED`; если бы отказ писался тем же
    значением, разобранные строки возвращались бы в очередь каждым
    отчётом, и она никогда бы не убывала.
    """
    _service(tenant, category, name="Ещё не смотрели")
    _service(
        tenant, category, name="Посмотрели и отказали",
        mapping_status=S.NOT_RECOMMENDABLE,
        mapping_confirmed_by=human,
        mapping_confirmed_at=timezone.now(),
        mapping_source_ref="разбор 56 услуг, строка 41",
    )

    queue = SalonService.objects.filter(mapping_status=S.UNMAPPED)
    decided = SalonService.objects.filter(mapping_status=S.NOT_RECOMMENDABLE)

    assert queue.count() == 1
    assert decided.count() == 1
    assert queue.get().name == "Ещё не смотрели"
