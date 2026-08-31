"""Тесты сида демо-салонов.

Правило контура, которому подчинён этот файл: **отрицательному
утверждению нужна положительная стража на тех же данных.** Рядом с
«дубли не появились» обязано стоять «а строки вообще создались, и их
столько-то» — иначе идемпотентность проходит на пустом наполнении, а
«подбор их не видит» проходит на пустой базе.

Дат-литералов здесь нет: всё, что связано со временем, — смещения от
``now`` (в сиде времени нет вовсе, кроме ``auto_now``).
"""
from __future__ import annotations

import json
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from services.goal_coverage import goal_master_coverage
from services.models import SalonService, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

SEEDS = Path(__file__).resolve().parents[1] / "seeds"
CANONICAL = SEEDS / "canonical_catalog_2026-07.json"
GOAL_OPTIONS = SEEDS / "goal_options_2026-08.json"
DEMO_SALONS = SEEDS / "demo_salons_2026-08.json"

# Цели, которые сид обязан открыть. Замер владельца 30.08: обе дают 0.
HOLES = ("event", "new_look")

DEMO_SLUGS = tuple(
    s["slug"] for s in json.loads(DEMO_SALONS.read_text(encoding="utf-8"))["salons"]
)


# --------------------------------------------------------------------- #
# фикстуры
# --------------------------------------------------------------------- #
@pytest.fixture
def catalog(db):
    """Настоящий канонический каталог и настоящие цели владельца.

    Не урезанная выборка: сид ложится на пары «категория → шаблон» из
    боевого файла, и подмена каталога тестовым доказывала бы разрешение
    имён, которых на контуре нет.
    """
    call_command("seed_canonical_catalog", "--file", str(CANONICAL), verbosity=0)
    call_command("seed_goal_options", "--file", str(GOAL_OPTIONS), verbosity=0)


@pytest.fixture
def pilot_like_baseline(catalog):
    """Салон формы «Формула тела»: только тело и массаж.

    Нужен, чтобы «красный» прогон означал именно дыру, а не пустую базу.
    На такой базе ``relax`` и ``body_shape`` обязаны быть НЕнулевыми —
    иначе ноль у ``event``/``new_look`` ничего не доказывает.
    """
    tenant = Tenant.all_objects.create(
        slug="baseline-body", name="Базовый тенант (тело)", is_active=True,
    )
    masters = []
    for idx in range(3):
        user = User.objects.create_user(
            username=f"baseline.master{idx}", role="specialist", tenant=tenant,
        )
        profile = user.specialist_profile
        profile.tenant = tenant
        profile.display_name = f"Базовый мастер {idx}"
        profile.address = "Москва, ул. Несуществующая, д. 0 (демо-данные)"
        profile.status = SpecialistProfile.ProfileStatus.ACTIVE
        profile.is_available = True
        profile.is_booking_enabled = True
        profile.rating = Decimal("4.7")
        profile.save()
        masters.append(profile)

    for cat_name, tpl_name in (
        ("Базовый ручной массаж", "Классический массаж всего тела"),
        ("Расслабляющие массажи", "Релакс-массаж"),
        ("Вакуумно-роликовые и лимфодренажные методики", "LPG-массаж тела"),
    ):
        template = ServiceTemplate.objects.filter(
            category__name=cat_name, name=tpl_name,
        ).first()
        if template is None:  # каталог мог переименовать позицию
            template = ServiceTemplate.objects.filter(
                category__name=cat_name,
            ).first()
        assert template is not None, f"нет ни одного шаблона в {cat_name}"
        salon_service = SalonService.objects.create(
            tenant=tenant, template=template, category=template.category,
            name=template.name, duration_minutes=60,
            base_price=Decimal("3000"), is_active=True,
        )
        for profile in masters:
            SpecialistService.objects.create(
                salon_service=salon_service, specialist=profile, tenant=tenant,
                price=Decimal("3000"), duration_minutes=60, is_active=True,
            )
    return tenant


def _run(*args) -> str:
    out = StringIO()
    call_command("seed_demo_salons", "--file", str(DEMO_SALONS), *args, stdout=out)
    return out.getvalue()


def _demo_rows() -> dict[str, int]:
    tenants = list(Tenant.all_objects.filter(slug__in=DEMO_SLUGS))
    return {
        "tenants": len(tenants),
        "specialists": SpecialistProfile.objects.filter(
            tenant__in=tenants,
        ).count(),
        "salon_services": SalonService.objects.filter(
            tenant__in=tenants,
        ).count(),
        "specialist_services": SpecialistService.objects.filter(
            tenant__in=tenants,
        ).count(),
    }


# --------------------------------------------------------------------- #
# 1. Премисса владельца о единственном флаге — проверяем, а не верим
# --------------------------------------------------------------------- #
@pytest.mark.django_db
def test_inactive_tenant_alone_does_not_hide_masters(catalog):
    """``Tenant.is_active=False`` НЕ прячет мастера от подбора.

    Владелец просил завести салоны выключенными, полагая, что обычный
    менеджер тенанта скроет их от всего кода. Менеджер скрывает строку
    от того, кто спрашивает про тенанты; ``RecommendationEngine``
    спрашивает про ``SpecialistProfile`` и к таблице тенантов не
    присоединяется вовсе.

    Тест фиксирует это как ФАКТ контура: пока он проходит, одного флага
    мало и три замка в сиде — не перестраховка. Если однажды движок
    научится фильтровать по тенанту, тест упадёт и его надо будет
    переписать в обратное утверждение — это желаемое падение.
    """
    from ai.application.services.recommendation_engine import (
        RecommendationEngine,
        RecommendationQuery,
    )

    tenant = Tenant.all_objects.create(
        slug="probe-inactive", name="Проба", is_active=False,
    )
    assert not Tenant.objects.filter(slug="probe-inactive").exists()

    user = User.objects.create_user(
        username="probe.master", role="specialist", tenant=tenant,
    )
    profile = user.specialist_profile
    profile.tenant = tenant
    profile.display_name = "Мастер выключенного тенанта"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.rating = Decimal("4.9")
    profile.save()

    template = ServiceTemplate.objects.filter(
        category__name="Стрижки", name="Женская стрижка",
    ).get()
    salon_service = SalonService.objects.create(
        tenant=tenant, template=template, category=template.category,
        name=template.name, duration_minutes=60, base_price=Decimal("3450"),
    )
    SpecialistService.objects.create(
        salon_service=salon_service, specialist=profile, tenant=tenant,
        price=Decimal("3450"), duration_minutes=60,
    )

    result = RecommendationEngine().recommend(
        RecommendationQuery(goal_category_ids=(template.category_id,)),
        use_cache=False,
    )
    names = [c.display_name for c in result.candidates]
    assert names == ["Мастер выключенного тенанта"], (
        "премисса владельца изменилась: is_active=False теперь что-то "
        "скрывает от подбора — перепроверь три замка в сиде"
    )

    # Положительная стража к обратному замку: как только выключен
    # status, тот же запрос на тех же данных отдаёт пусто. То есть
    # проверка умеет отличать «видно» от «не видно».
    profile.status = SpecialistProfile.ProfileStatus.PENDING
    profile.save(update_fields=["status"])
    result = RecommendationEngine().recommend(
        RecommendationQuery(goal_category_ids=(template.category_id,)),
        use_cache=False,
    )
    assert [c.display_name for c in result.candidates] == []


# --------------------------------------------------------------------- #
# 2. Красный прогон -> зелёный: дыры event / new_look
# --------------------------------------------------------------------- #
@pytest.mark.django_db
def test_seed_then_activate_closes_event_and_new_look(pilot_like_baseline):
    """До сида — ноль по ``event``/``new_look``; после включения — не ноль."""
    before = goal_master_coverage()

    # КРАСНЫЙ. Ноль именно у двух целей, а не везде: положительная
    # стража — покрытие, которое у базового салона уже есть.
    for key in HOLES:
        assert before[key] == 0, f"{key} должен быть пуст до сида: {before}"
    assert before["relax"] > 0, (
        f"базовый салон обязан покрывать relax, иначе ноль у {HOLES} "
        f"ничего не доказывает: {before}"
    )
    assert before["body_shape"] > 0, before

    _run("--apply")
    _run("--activate", "--apply")

    after = goal_master_coverage()

    # ЗЕЛЁНЫЙ.
    for key in HOLES:
        assert after[key] > 0, f"{key} остался пуст после сида: {after}"

    # Ни одна цель не просела: сид только добавляет.
    for key, was in before.items():
        assert after[key] >= was, f"{key}: {was} -> {after[key]}"

    # Положительная стража на объём: строки не просто «появились», их
    # ровно столько, сколько объявлено в файле сида.
    document = json.loads(DEMO_SALONS.read_text(encoding="utf-8"))
    expected_specialists = sum(len(s["specialists"]) for s in document["salons"])
    expected_services = sum(len(s["services"]) for s in document["salons"])
    expected_links = sum(
        len(v["by"]) for s in document["salons"] for v in s["services"]
    )
    assert _demo_rows() == {
        "tenants": len(document["salons"]),
        "specialists": expected_specialists,
        "salon_services": expected_services,
        "specialist_services": expected_links,
    }


# --------------------------------------------------------------------- #
# 3. Замки: записано, но подбору не видно
# --------------------------------------------------------------------- #
@pytest.mark.django_db
def test_written_data_stays_invisible_until_activate(pilot_like_baseline):
    """``--apply`` без ``--activate`` наполняет базу, но не выдачу."""
    before = goal_master_coverage()
    _run("--apply")

    # Положительная стража: строки действительно созданы. Без неё
    # «покрытие не изменилось» доказывалось бы пустой базой.
    rows = _demo_rows()
    assert rows["tenants"] == len(DEMO_SLUGS)
    assert rows["specialists"] > 0
    assert rows["salon_services"] > 0
    assert rows["specialist_services"] > 0

    assert goal_master_coverage() == before, (
        "демо-данные протекли в подбор до явного --activate"
    )

    # Все три замка на месте.
    demo_tenants = list(Tenant.all_objects.filter(slug__in=DEMO_SLUGS))
    assert all(not t.is_active for t in demo_tenants)
    assert not Tenant.objects.filter(slug__in=DEMO_SLUGS).exists()
    profiles = SpecialistProfile.objects.filter(tenant__in=demo_tenants)
    assert not profiles.exclude(
        status=SpecialistProfile.ProfileStatus.PENDING,
    ).exists()
    assert not profiles.filter(is_booking_enabled=True).exists()

    # И только теперь — открывается.
    _run("--activate", "--apply")
    after = goal_master_coverage()
    for key in HOLES:
        assert after[key] > 0, after


# --------------------------------------------------------------------- #
# 4. Идемпотентность — с положительной стражей
# --------------------------------------------------------------------- #
@pytest.mark.django_db
def test_second_run_adds_nothing_but_the_rows_are_there(catalog):
    _run("--apply")
    first = _demo_rows()

    # Стража: первый прогон действительно наполнил базу. Иначе
    # «второй прогон ничего не добавил» — тавтология.
    assert first["tenants"] == len(DEMO_SLUGS)
    assert first["specialists"] == 22
    assert first["salon_services"] == 171
    assert first["specialist_services"] == 232

    output = _run("--apply")
    assert _demo_rows() == first, "повторный прогон наплодил дубли"
    assert "salons=0" in output and "masters=0" in output
    assert "reused (idempotent)" in output


# --------------------------------------------------------------------- #
# 5. Сухой прогон
# --------------------------------------------------------------------- #
@pytest.mark.django_db
def test_dry_run_writes_nothing_yet_predicts_the_closure(pilot_like_baseline):
    output = _run()

    assert _demo_rows() == {
        "tenants": 0, "specialists": 0,
        "salon_services": 0, "specialist_services": 0,
    }, "сухой прогон записал в базу"
    assert "nothing written" in output

    # Прогноз — измерение в откатанной транзакции, а не мнение автора:
    # он обязан назвать обе закрытые дыры.
    assert output.count("hole closed") >= len(HOLES)
    for key in HOLES:
        assert key in output
    # И обязан показать ненулевой объём — иначе «дыра закрыта» без
    # единой строки было бы возможно.
    assert "salons=5" in output
    assert "masters=22" in output


# --------------------------------------------------------------------- #
# 6. Стражи файла сида
# --------------------------------------------------------------------- #
@pytest.mark.django_db
def test_refuses_to_write_into_a_protected_tenant(catalog, tmp_path):
    """Слаг боевого пилота в файле сида — отказ, а не запись.

    ``formula-tela`` существует ещё до теста: его заводит миграция
    ``tenants.0003_seed_default_tenants``. Поэтому доказывать надо не
    «тенанта нет», а «в него ничего не дописали» — именно этим опасно
    совпадение слага на живом контуре.
    """
    document = json.loads(DEMO_SALONS.read_text(encoding="utf-8"))
    document["salons"] = document["salons"][:1]
    document["salons"][0]["slug"] = "formula-tela"
    path = tmp_path / "clash.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    live = Tenant.all_objects.get(slug="formula-tela")
    with pytest.raises(CommandError, match="protected tenant"):
        call_command("seed_demo_salons", "--file", str(path), "--apply")

    assert SpecialistProfile.objects.filter(tenant=live).count() == 0
    assert SalonService.objects.filter(tenant=live).count() == 0
    assert SpecialistService.objects.filter(tenant=live).count() == 0
    live.refresh_from_db()
    assert live.name == "Формула тела"


@pytest.mark.django_db
def test_refuses_a_service_that_is_not_in_the_canonical_catalog(catalog, tmp_path):
    document = json.loads(DEMO_SALONS.read_text(encoding="utf-8"))
    document["salons"] = document["salons"][:1]
    document["salons"][0]["services"] = [{
        "category": "Стрижки",
        "template": "Стрижка, которой нет в каноне",
        "price": 1000,
        "duration_minutes": 30,
        "price_src": "derived",
        "by": [document["salons"][0]["specialists"][0]["ref"]],
    }]
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CommandError, match="Unresolved"):
        call_command("seed_demo_salons", "--file", str(path), "--apply")
    assert _demo_rows()["tenants"] == 0


@pytest.mark.django_db
def test_activate_refuses_when_nothing_was_seeded(catalog):
    with pytest.raises(CommandError, match="do not exist yet"):
        call_command(
            "seed_demo_salons", "--file", str(DEMO_SALONS),
            "--activate", "--apply",
        )


# --------------------------------------------------------------------- #
# 7. Персональных данных не заводим
# --------------------------------------------------------------------- #
@pytest.mark.django_db
def test_no_demo_master_gets_a_phone_number(catalog):
    _run("--apply")
    demo_tenants = list(Tenant.all_objects.filter(slug__in=DEMO_SLUGS))
    users = User.objects.filter(tenant__in=demo_tenants)

    # Стража: пользователи вообще созданы.
    assert users.count() == 22
    # Любой правдоподобный номер — чей-то настоящий номер.
    assert not users.exclude(phone__isnull=True).exists()
    assert not users.filter(is_verified=True).exists()
    # Выдуманных отзывов тоже нет.
    profiles = SpecialistProfile.objects.filter(tenant__in=demo_tenants)
    assert not profiles.exclude(reviews_count=0).exists()


@pytest.mark.django_db
def test_every_master_carries_a_named_specialty(catalog):
    """Роль мастера доезжает до профиля, а не лежит в файле мёртвой.

    «Подбору не из чего выбирать» — это в равной мере про число мастеров
    и про то, что они разные. Салон из семи безымянных специальностей
    выбор не создаёт.
    """
    _run("--apply")
    demo_tenants = list(Tenant.all_objects.filter(slug__in=DEMO_SLUGS))
    profiles = SpecialistProfile.objects.filter(tenant__in=demo_tenants)

    assert profiles.count() == 22
    assert not profiles.filter(bio="").exists()
    assert not profiles.filter(experience_years=0).exists()

    document = json.loads(DEMO_SALONS.read_text(encoding="utf-8"))
    for salon in document["salons"]:
        tenant = Tenant.all_objects.get(slug=salon["slug"])
        roles = {p["role"] for p in salon["specialists"]}
        assert roles, salon["slug"]
        for person in salon["specialists"]:
            profile = SpecialistProfile.objects.get(
                tenant=tenant, display_name=person["display_name"],
            )
            assert profile.bio.startswith(person["role"]), profile.bio


@pytest.mark.django_db
def test_every_bookable_row_hangs_on_a_leaf_category(catalog):
    """Услуга обязана висеть на категории шаблона, а не на корне.

    Цели курируются на корнях и раскрываются вниз (DRF-1308). Услуга,
    посаженная на корень, не нашлась бы через цель — именно так дыра и
    появляется.
    """
    _run("--apply")
    demo_tenants = list(Tenant.all_objects.filter(slug__in=DEMO_SLUGS))
    rows = SalonService.objects.filter(
        tenant__in=demo_tenants,
    ).select_related("category", "template__category")

    assert rows.count() == 171
    mismatched = [
        r.name for r in rows if r.category_id != r.template.category_id
    ]
    assert mismatched == []
    assert not rows.filter(category__isnull=True).exists()
