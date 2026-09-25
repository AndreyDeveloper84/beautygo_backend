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

import re
import uuid
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate
from services.owner_confirmed_mapping import (
    CONFIRMED,
    DOCUMENT,
    EXPECTED_BY_SALON,
    OWNER_LIST_RULE,
    OWNER_LIST_RULE_VERSION,
    OWNER_LIST_SOURCE_REF,
)
from tenants.models import Tenant

pytestmark = pytest.mark.django_db


DEFAULT_SLUG = "formula-tela"
_SLUG_CELL = re.compile(r"^\*{0,2}`(?P<slug>[a-z0-9-]+)`\*{0,2}$")
_CODE_CELL = re.compile(r"^`(?P<code>\d+(?:\.\d+)+)`$")


def read_document() -> list[tuple[str, str, str]]:
    """Тройки прямо из документа основания.

    Фикстуры строятся ОТСЮДА, а не рядом. Первая редакция создавала все 36
    строк у одного салона — то есть изготавливала реальность, которой нет, —
    и узел «36 получают связь» был зелен потому, что ФИКСТУРА СОГЛАСНА С
    КОДОМ, а не код с документом. Пока данные для проверки берутся из того же
    источника, что и для работы, такой ответ невозможен.

    Разбор терпим к двум формам таблицы: поправка 25.09 добавила столбец
    «Салон» только в раздел массажей, и то не всем строкам. Где слага нет —
    он `formula-tela`, как прямо говорит поправка.
    """
    text = DOCUMENT.read_text(encoding="utf-8")
    start = text.index("## Подтверждено")
    end = text.index("## НЕ подтверждено")
    section = text[start:end]
    out: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("- ") or cells[0] in ("Салон", "Услуга салона"):
            continue
        m = _SLUG_CELL.match(cells[0])
        rest = cells[1:] if m else cells
        code = _CODE_CELL.match(rest[1])
        assert code, f"не код в строке документа: {line!r}"
        out.append((m.group("slug") if m else DEFAULT_SLUG, rest[0].strip("* "), code.group("code")))
    return out


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


def _salons() -> dict[str, Tenant]:
    """ОБА салона документа, а не один.

    Защищённый слаг может быть уже заведён общей фикстурой репозитория —
    берём существующего, иначе узел падал бы на уникальности, а не на предмете.
    """
    out: dict[str, Tenant] = {}
    for slug in EXPECTED_BY_SALON:
        tenant, _ = Tenant.objects.get_or_create(
            slug=slug, defaults={"name": slug, "kind": Tenant.Kind.SALON}
        )
        out[slug] = tenant
    return out


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


def _whole_list() -> dict[tuple[str, str], SalonService]:
    """Весь список — строки ОБОИХ салонов и шаблоны канона.

    Строится из `read_document()`, а не из `CONFIRMED`: см. довод там.
    """
    salons = _salons()
    for code in sorted({c for _, _, c in read_document()}):
        if not ServiceTemplate.objects.filter(canonical_code=code).exists():
            _tpl(code)
    return {
        (slug, name): _row(salons[slug], name)
        for slug, name, _ in read_document()
    }


def _run(**options) -> str:
    out = StringIO()
    call_command("confirm_owner_mapping", stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


def _run_expecting_halt(**options) -> tuple[str, str]:
    """Прогон, который обязан упереться в ворота, — с ЕГО ВЫВОДОМ.

    Исключения печатаются до ворот, и без вывода узел проверил бы лишь факт
    отказа, а не то, названа ли причина поимённо. «Останов произошёл» и
    «оператор понял, почему» — разные утверждения.
    """
    out = StringIO()
    with pytest.raises(CommandError) as err:
        call_command("confirm_owner_mapping", stdout=out, stderr=StringIO(), **options)
    return out.getvalue(), str(err.value)


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

        Сверяются ТРОЙКИ, включая салон. Прежняя редакция сверяла только пары
        «услуга → код» — и пропустила то, что одна строка принадлежит другому
        салону, хотя документ говорит это прямым текстом двумя строками выше
        таблицы. Сверка, не покрывающая поле, по которому команда адресует,
        не сверяет ничего важного.
        """
        assert DOCUMENT.exists(), f"документ основания не на месте: {DOCUMENT}"
        assert read_document() == [tuple(x) for x in CONFIRMED], (
            "список в коде разошёлся с документом основания"
        )

    def test_the_list_holds_exactly_thirty_six(self) -> None:
        # Положительная пара к утверждениям об отсутствии ниже: если список
        # однажды опустеет, все проверки «не тронуто» станут вакуумными.
        assert len(CONFIRMED) == 36
        assert len({(s, n) for s, n, _ in CONFIRMED}) == 36, (
            "пары «салон + услуга» обязаны быть различны"
        )

    def test_the_distribution_by_salon_matches_the_correction(self) -> None:
        """35 + 1, а не 36 у одного салона.

        Ровно эта посылка и была неверна в первой редакции: `--apply` упал бы
        на 35 из 36, а ворота напечатали бы ложную причину.
        """
        from collections import Counter

        assert dict(Counter(s for s, _, _ in CONFIRMED)) == EXPECTED_BY_SALON


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
        rows = _whole_list()

        _run(apply=True)

        for slug, name, code in CONFIRMED:
            row = rows[(slug, name)]
            row.refresh_from_db()
            assert row.mapping_status == SalonService.MappingStatus.VERIFIED, name
            assert row.template is not None, name
            assert row.template.canonical_code == code, name

    def test_provenance_is_read_from_the_field_on_every_row(self) -> None:
        rows = _whole_list()

        _run(apply=True)

        for key in rows:
            row = rows[key]
            row.refresh_from_db()
            assert row.mapping_confirmed_rule == OWNER_LIST_RULE, key
            assert row.mapping_rule_version == OWNER_LIST_RULE_VERSION, key
            assert row.mapping_source_ref == OWNER_LIST_SOURCE_REF, key
            assert row.mapping_confirmed_at is not None, key
            # Подтверждает правило — значит «кто» пуст: схема запрещает оба.
            assert row.mapping_confirmed_by_id is None, key

    def test_the_unconfirmed_stay_unmapped(self) -> None:
        """Вторая сторона, и она важнее первой.

        Без неё мы измерили бы «что-то записалось», а не «записалось верное».
        Три из этих услуг в каноне отсутствуют вовсе — дыра канона, §77 п.54.
        """
        _whole_list()
        salons = _salons()
        untouched = [
            _row(salons[DEFAULT_SLUG], name)
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
        _whole_list()
        salons = _salons()
        stranger = _row(salons[DEFAULT_SLUG], "Услуга, которой нет в списке владельца")

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
        _whole_list()
        other = Tenant.objects.create(
            slug=f"other-{uuid.uuid4().hex[:8]}", name="Соседний", kind=Tenant.Kind.SALON
        )
        twin = _row(other, CONFIRMED[0][1])

        _run(apply=True)

        twin.refresh_from_db()
        assert twin.mapping_status == SalonService.MappingStatus.UNMAPPED
        assert twin.template_id is None

    def test_only_the_two_allowed_fields_change(self) -> None:
        """Имя, цена, длительность и активность — салонные, их не трогают."""
        rows = _whole_list()
        slug, name = CONFIRMED[0][0], CONFIRMED[0][1]
        row = rows[(slug, name)]
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
        _whole_list()

        out = _run()

        assert "36" in out
        assert "сухой прогон" in out.lower()

    def test_a_count_other_than_thirty_six_halts(self) -> None:
        """Останов, а не «применим сколько нашлось».

        Несовпадение значит, что данные на стенде уехали с 25.09 — и тогда
        пересматривать надо список, а не додавливать команду.
        """
        rows = _whole_list()
        rows[(CONFIRMED[0][0], CONFIRMED[0][1])].delete()

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
        rows = _whole_list()
        rows[(CONFIRMED[0][0], CONFIRMED[0][1])].delete()
        rest = [r for k, r in rows.items() if k != (CONFIRMED[0][0], CONFIRMED[0][1])]
        before = _snapshot(rest)

        with pytest.raises(CommandError) as err:
            _run(apply=True)
        assert "36" in str(err.value), f"отказ не про число кандидатов: {err.value}"

        assert _snapshot(rest) == before


class TestDryRunChangesNothing:
    def test_state_before_and_after_is_identical(self) -> None:
        """Сравнением состояния, а не доверием ключу."""
        rows = _whole_list()
        listed = list(rows.values())
        before = _snapshot(listed)

        _run()

        assert _snapshot(listed) == before


class TestIdempotence:
    def test_the_second_run_changes_nothing_including_the_date(self) -> None:
        rows = _whole_list()

        _run(apply=True)
        listed = list(rows.values())
        after_first = _snapshot(listed)

        _run(apply=True)

        assert _snapshot(listed) == after_first


class TestWhatMustNotBeOverwritten:
    """Состояния, где кандидатов формально хватает, а запись была бы неверна.

    Все четыре найдены ревью, и все четыре опаснее недобора: число сходится
    или почти сходится, ворота довольны, а строка уходит не туда — или чужое
    решение стирается вместе с его автором.
    """

    def test_a_duplicate_name_halts_and_is_named(self) -> None:
        """Дубль имени останавливает прогон, а не схлопывается молча.

        Схема его разрешает: уникальна только тройка (салон, шаблон, имя), а
        у всех целей шаблон пуст. Словарь по имени оставил бы произвольного
        двойника — и вот что важно: ЧИСЛО ПРИ ЭТОМ СОШЛОСЬ БЫ. Первая
        редакция узла ждала останова, а команда доходила до конца: 36
        перечисленных нашлись, дубль ушёл в пропуски, ворота на числе такой
        промах не ловят по построению.

        Чинилась команда, а не узел. Владелец подтвердил «строку с таким
        названием»; если таких две, какую именно — неизвестно, и запись в
        первую попавшуюся была бы догадкой. Правило то же, по которому девять
        неподтверждённых остаются пустыми: пустая связь честнее неверной.
        """
        rows = _whole_list()
        salons = _salons()
        slug, name = CONFIRMED[0][0], CONFIRMED[0][1]
        twin = _row(salons[slug], name)

        original = rows[(slug, name)]
        out, _ = _run_expecting_halt(apply=True)

        assert "ДУБЛЬ ИМЕНИ" in out, out
        assert name in out
        # Обе строки: и двойник, и ИСХОДНАЯ. Останов обязан удержать запись
        # целиком — иначе «остановились» значило бы «успели половину».
        for row in (twin, original):
            row.refresh_from_db()
            assert row.mapping_status == SalonService.MappingStatus.UNMAPPED
            assert row.template_id is None

    def test_a_row_decided_by_a_human_is_not_touched(self) -> None:
        """Решение человека не переигрывается, и он не теряется.

        `mapping_review` держит это инвариантом — «уже решено, не
        переигрывается», — и оператору через админку такое недоступно.
        Командой обходить инвариант нельзя тем более: тут боевой салон.
        """
        from django.contrib.auth import get_user_model

        rows = _whole_list()
        slug, name, code = CONFIRMED[0]
        row = rows[(slug, name)]
        человек = get_user_model().objects.create(username=f"u{uuid.uuid4().hex[:8]}")
        SalonService.objects.filter(pk=row.pk).update(
            template=ServiceTemplate.objects.get(canonical_code=code),
            mapping_status=SalonService.MappingStatus.VERIFIED,
            mapping_confirmed_by=человек,
            mapping_confirmed_at=timezone.now(),
            mapping_source_ref="решение человека",
        )

        out, _ = _run_expecting_halt(apply=True)

        assert "УЖЕ РЕШЕНО" in out, out
        row.refresh_from_db()
        assert row.mapping_confirmed_by_id == человек.pk, "человек снят — этого нельзя"
        assert row.mapping_source_ref == "решение человека", "основание затёрто"

    def test_a_row_confirmed_by_another_rule_is_not_touched(self) -> None:
        """Чужое правило — тоже «уже решено», и переписывать его нельзя."""
        rows = _whole_list()
        slug, name, code = CONFIRMED[0]
        row = rows[(slug, name)]
        SalonService.objects.filter(pk=row.pk).update(
            template=ServiceTemplate.objects.get(canonical_code=code),
            mapping_status=SalonService.MappingStatus.VERIFIED,
            mapping_confirmed_rule="master_selected_from_canon",
            mapping_rule_version="1",
            mapping_confirmed_at=timezone.now(),
            mapping_source_ref="master_select:42",
        )

        out, _ = _run_expecting_halt(apply=True)

        assert "УЖЕ РЕШЕНО" in out, out
        row.refresh_from_db()
        assert row.mapping_confirmed_rule == "master_selected_from_canon"
        assert row.mapping_source_ref == "master_select:42"

    def test_a_provisional_canon_does_not_get_verified(self) -> None:
        """Черновой канон не получает `verified`.

        Админский путь такое прямо отказывает, а `check_canon_invariants`
        считает `verified_on_provisional` размером дыры. Расширять дыру
        командой нельзя.
        """
        rows = _whole_list()
        slug, name, code = CONFIRMED[0]
        ServiceTemplate.objects.filter(canonical_code=code).update(
            lifecycle="provisional"
        )

        out, _ = _run_expecting_halt(apply=True)

        assert "канон не approved" in out, out
        row = rows[(slug, name)]
        row.refresh_from_db()
        assert row.mapping_status == SalonService.MappingStatus.UNMAPPED
        assert row.template_id is None
