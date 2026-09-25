"""DRF-2516 — поставить 36 связей по списку, подтверждённому владельцем 25.09.

Список закрыт и подтверждён дословно — «да по списку». Лист не про то, чтобы
написать 36 строк: это пять минут. Он про то, что **связь без указания, кто её
подтвердил, через месяц неотличима от машинной догадки** (DRF-2408).

Узлы держат ровно то, что названо сторожами листа:

* **адресность — свойство, а не слово:** строка, которой нет в списке, командой
  не затрагивается. Проверяется соседством: рядом с перечисленной стоит
  непepечисленная, у которой всё остальное совпадает;
* **обе стороны:** 36 перечисленных получают связь **и** неподтверждённые
  остаются `unmapped`. Второе важнее: без него мы измерили бы «что-то
  записалось», а не «записалось верное»;
* **число до изменения:** кандидатов ровно 36, иначе останов. Не «применим
  сколько нашлось»: несовпадение значит, что данные уехали с 25.09, и тогда
  список надо пересматривать, а не додавливать;
* **провенанс читается ИЗ ПОЛЯ**, а не из докстроки, и правило — **своё**:
  ни правило выбора мастера, ни правило сида к этим строкам не применяется;
* **идемпотентность:** второй прогон меняет ноль строк, включая дату;
* **сухой прогон ничего не меняет** — проверяется сравнением состояния до и
  после, а не доверием ключу.
"""

from __future__ import annotations

import uuid
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate
from services.owner_confirmed_mapping import (
    CONFIRMED,
    DOCUMENT,
    OWNER_LIST_RULE,
    OWNER_LIST_RULE_VERSION,
    OWNER_LIST_SOURCE_REF,
    OWNER_LIST_TENANT_SLUG,
)
from tenants.models import Tenant

pytestmark = pytest.mark.django_db


def _cat(name: str = "Лазерная эпиляция") -> ServiceCategory:
    return ServiceCategory.objects.get_or_create(name=name)[0]


def _tpl(code: str) -> ServiceTemplate:
    """Шаблон канона с заданным кодом — по нему команда и находит цель."""
    return ServiceTemplate.objects.create(
        category=_cat(),
        name=f"Канон {code}",
        name_short="Канон",
        canonical_code=code,
        lifecycle="approved",
        requires_health_check=False,
        approved_rule="test",
        approval_rule_version="1",
        approved_at=timezone.now(),
        approval_source_ref="fixture",
    )


def _salon() -> Tenant:
    tenant, _ = Tenant.objects.get_or_create(
        slug=OWNER_LIST_TENANT_SLUG,
        defaults={"name": "Формула тела", "kind": Tenant.Kind.SALON},
    )
    return tenant


def _row(tenant: Tenant, name: str, **over) -> SalonService:
    """Непривязанная строка салона — то состояние, из которого команда ведёт."""
    fields = {
        "tenant": tenant,
        "template": None,
        "category": _cat(),
        "name": name,
        "duration_minutes": 60,
        "source": SalonService.Source.MANUAL,
        "mapping_status": SalonService.MappingStatus.UNMAPPED,
    }
    fields.update(over)
    return SalonService.objects.create(**fields)


def _whole_list(tenant: Tenant) -> dict[str, SalonService]:
    """Весь подтверждённый список — и строки салона, и шаблоны канона."""
    for _, code in {(n, c) for n, c in CONFIRMED}:
        if not ServiceTemplate.objects.filter(canonical_code=code).exists():
            _tpl(code)
    return {name: _row(tenant, name) for name, _ in CONFIRMED}


def _run(**options) -> str:
    out = StringIO()
    call_command("confirm_owner_mapping", stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


def _snapshot(rows) -> list[tuple]:
    return [
        (
            r.pk,
            r.template_id,
            r.mapping_status,
            r.mapping_confirmed_rule,
            r.mapping_rule_version,
            r.mapping_confirmed_at,
            r.mapping_source_ref,
        )
        for r in SalonService.objects.filter(
            pk__in=[x.pk for x in rows]
        ).order_by("pk")
    ]


class TestTheListIsTheDocument:
    def test_the_list_matches_the_document(self) -> None:
        """Список и документ — одна правда, и это проверяется, а не обещается.

        Разойдясь, они дали бы худший из возможных исходов: связь, у которой
        провенанс ссылается на документ, где написано ДРУГОЕ.
        """
        assert DOCUMENT.exists(), f"документ основания не на месте: {DOCUMENT}"
        text = DOCUMENT.read_text(encoding="utf-8")
        section = text[text.index("## Подтверждено") : text.index("## НЕ подтверждено")]

        import re

        rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*`(\d+(?:\.\d+)+)`\s*\|", section, re.M)
        assert list(rows) == [list(p) for p in map(list, CONFIRMED)] or rows == [
            (n, c) for n, c in CONFIRMED
        ], "список в коде разошёлся с документом основания"

    def test_the_list_holds_exactly_thirty_six(self) -> None:
        # Положительная пара к утверждениям об отсутствии ниже: если список
        # однажды опустеет, все проверки «не тронуто» станут вакуумными.
        assert len(CONFIRMED) == 36
        assert len({n for n, _ in CONFIRMED}) == 36, "названия услуг обязаны быть различны"


class TestTheRuleNamesItsOrigin:
    def test_rule_name_does_not_claim_a_master_or_a_salon_owner(self) -> None:
        """Имя правила не присваивает подтверждение не тому человеку.

        `master_selected_from_canon` сказало бы «мастер выбрал», чего не было.
        А `owner_*` в этой кодовой базе означает владельца САЛОНА (`is_owner`),
        и такое имя приписало бы подтверждение салону. Подтверждал владелец
        продукта, и имя обязано говорить именно это.
        """
        assert OWNER_LIST_RULE == "product_owner_confirmed_list"
        assert "master" not in OWNER_LIST_RULE
        assert not OWNER_LIST_RULE.startswith("owner")
        assert OWNER_LIST_RULE_VERSION

    def test_source_ref_points_inside_this_repository(self) -> None:
        """Основание обязано открываться у того, кто читает каталог.

        Ссылка на файл соседнего репозитория (без удалённых адресов, да ещё и
        неотслеживаемый) — это провенанс по имени, а не по сути.
        """
        assert OWNER_LIST_SOURCE_REF == "docs/MAPPING_SERVICES_OWNER_CONFIRMED_2026-09-25.md"
        assert len(OWNER_LIST_SOURCE_REF) <= 200, "поле основания не длиннее 200"
        assert DOCUMENT.exists()


class TestBothSides:
    def test_the_listed_thirty_six_get_the_link(self) -> None:
        salon = _salon()
        rows = _whole_list(salon)

        _run(apply=True)

        for name, code in CONFIRMED:
            row = rows[name]
            row.refresh_from_db()
            assert row.mapping_status == SalonService.MappingStatus.VERIFIED, name
            assert row.template is not None, name
            assert row.template.canonical_code == code, name

    def test_provenance_is_read_from_the_field_on_every_row(self) -> None:
        salon = _salon()
        rows = _whole_list(salon)

        _run(apply=True)

        for name in rows:
            row = rows[name]
            row.refresh_from_db()
            assert row.mapping_confirmed_rule == OWNER_LIST_RULE, name
            assert row.mapping_rule_version == OWNER_LIST_RULE_VERSION, name
            assert row.mapping_source_ref == OWNER_LIST_SOURCE_REF, name
            assert row.mapping_confirmed_at is not None, name
            # Подтверждает правило — значит «кто» пуст: схема запрещает оба.
            assert row.mapping_confirmed_by_id is None, name

    def test_the_unconfirmed_stay_unmapped(self) -> None:
        """Вторая сторона, и она важнее первой.

        Без неё мы измерили бы «что-то записалось», а не «записалось верное».
        Три из этих услуг в каноне отсутствуют вовсе — дыра канона, §77 п.54.
        """
        salon = _salon()
        _whole_list(salon)
        untouched = [
            _row(salon, name)
            for name in (
                "Биоэнергетический массаж",
                "Биоэнергетический массаж детский",
                "Парный массаж",
                "Бикини тотальное",
                "Массаж в 4 руки",
                "Пилинг PROBIO PEEL",
            )
        ]

        _run(apply=True)

        for row in untouched:
            row.refresh_from_db()
            assert row.mapping_status == SalonService.MappingStatus.UNMAPPED, row.name
            assert row.template_id is None, row.name
            assert row.mapping_source_ref == "", row.name


class TestAddressingIsAProperty:
    def test_a_row_outside_the_list_is_not_touched(self) -> None:
        """Ложный вход: всё совпадает, кроме присутствия в списке.

        Соседство здесь — суть проверки. Строка стоит в том же салоне, с тем
        же источником и тем же статусом; единственное отличие — её нет в
        подтверждённом списке. Отбор обязан отличать по списку, а не по
        «похоже на непривязанную услугу этого салона».
        """
        salon = _salon()
        _whole_list(salon)
        stranger = _row(salon, "Услуга, которой нет в списке владельца")

        _run(apply=True)

        stranger.refresh_from_db()
        assert stranger.mapping_status == SalonService.MappingStatus.UNMAPPED
        assert stranger.template_id is None
        assert stranger.mapping_source_ref == ""

    def test_another_salon_with_the_same_service_name_is_not_touched(self) -> None:
        """Одноимённая услуга ЧУЖОГО салона — не предмет этого списка.

        Список подтверждён для конкретного салона. Отбор по одному названию
        задел бы тёзку у соседа, и провенанс сказал бы, что владелец её
        подтверждал, — чего не было.
        """
        salon = _salon()
        _whole_list(salon)
        other = Tenant.objects.create(
            slug=f"other-{uuid.uuid4().hex[:8]}", name="Соседний", kind=Tenant.Kind.SALON
        )
        twin = _row(other, CONFIRMED[0][0])

        _run(apply=True)

        twin.refresh_from_db()
        assert twin.mapping_status == SalonService.MappingStatus.UNMAPPED
        assert twin.template_id is None

    def test_only_the_two_allowed_fields_change(self) -> None:
        """Имя, цена, длительность и активность — салонные, их не трогают."""
        salon = _salon()
        rows = _whole_list(salon)
        name = CONFIRMED[0][0]
        row = rows[name]
        SalonService.objects.filter(pk=row.pk).update(
            base_price="1234.00", duration_minutes=45, is_active=False
        )

        _run(apply=True)

        row.refresh_from_db()
        assert str(row.base_price) == "1234.00"
        assert row.duration_minutes == 45
        assert row.is_active is False
        assert row.name == name


class TestTheCountIsShownBeforeTheChange:
    def test_dry_run_prints_exactly_thirty_six(self) -> None:
        salon = _salon()
        _whole_list(salon)

        out = _run()

        assert "36" in out
        assert "сухой прогон" in out.lower()

    def test_a_count_other_than_thirty_six_halts(self) -> None:
        """Останов, а не «применим сколько нашлось».

        Несовпадение значит, что данные на стенде уехали с 25.09 — и тогда
        пересматривать надо список, а не додавливать команду.
        """
        salon = _salon()
        rows = _whole_list(salon)
        rows[CONFIRMED[0][0]].delete()

        with pytest.raises(CommandError) as err:
            _run(apply=True)
        assert "36" in str(err.value)

    def test_the_halt_happens_before_anything_is_written(self) -> None:
        """Останов обязан быть ДО записи, иначе он половинчатый.

        Ожидание «поднялся CommandError» само по себе ничего не значит:
        `call_command` бросает его же на «неизвестная команда», и в красной
        фазе узел зеленел бы, пока команды ещё нет. Поэтому требуется, чтобы
        отказ был ИМЕННО про число, — критерий не должен быть выполним ничем,
        кроме проверяемого предмета.
        """
        salon = _salon()
        rows = _whole_list(salon)
        rows[CONFIRMED[0][0]].delete()
        rest = [r for n, r in rows.items() if n != CONFIRMED[0][0]]
        before = _snapshot(rest)

        with pytest.raises(CommandError) as err:
            _run(apply=True)
        assert "36" in str(err.value), f"отказ не про число кандидатов: {err.value}"

        assert _snapshot(rest) == before


class TestDryRunChangesNothing:
    def test_state_before_and_after_is_identical(self) -> None:
        """Сравнением состояния, а не доверием ключу."""
        salon = _salon()
        rows = _whole_list(salon)
        listed = list(rows.values())
        before = _snapshot(listed)

        _run()

        assert _snapshot(listed) == before


class TestIdempotence:
    def test_the_second_run_changes_nothing_including_the_date(self) -> None:
        salon = _salon()
        rows = _whole_list(salon)

        _run(apply=True)
        listed = list(rows.values())
        after_first = _snapshot(listed)

        _run(apply=True)

        assert _snapshot(listed) == after_first
