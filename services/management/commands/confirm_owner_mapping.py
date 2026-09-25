"""``manage.py confirm_owner_mapping [--apply]`` — поставить 36 связей по списку,
подтверждённому владельцем продукта 25.09.2026 (DRF-2516).

Лист не про то, чтобы написать 36 строк: это пять минут. Он про то, что **связь
без указания, кто её подтвердил, через месяц неотличима от машинной догадки** —
и именно из-за таких начался весь разговор о привязке услуг (DRF-2408).

Что команда берёт
-----------------

Строку, у которой **всё** из перечисленного:

* салон — ``formula-tela`` (назван явно, см. ниже);
* название **дословно совпадает** с названием из подтверждённого списка;
* привязки ещё нет (``template`` пуст) либо уже стоит ровно та, что в списке.

Отбор идёт **по списку, а не по признаку**. Это и есть адресность: «все
непривязанные строки этого салона» задели бы девять неподтверждённых, а
«похожие по названию» — тёзку у соседнего салона.

Почему боевой салон, который соседняя команда защищает
-------------------------------------------------------

``confirm_seeded_links`` держит ``formula-tela`` в ``PROTECTED_SLUGS``: там
настоящие записи настоящих людей, и правило про демонстрационный набор к ним
не применяется. Здесь — другой случай и другое основание: владелец продукта
посмотрел **поимённо** и подтвердил **тридцать шесть конкретных строк**.

Поэтому защита не импортируется и не обходится молча: команда вообще не
работает «по салону». Она работает по списку имён, и её область равна этому
списку. Именно из-за боевого салона здесь и стоят останов на числе, запись
«было → стало» и запрет трогать что-либо, кроме двух полей.

Почему своё имя правила
-----------------------

``rule_confirmation`` в ``services/offer_selection.py`` зашивает
``master_selected_from_canon`` жёстко — параметра там нет. Позвать его как есть
значило бы написать «мастер выбрал из канона» там, где мастер ничего не
выбирал: подлог провенанса, дословно тот, что назван в DRF-2408.

Имя ``product_owner_confirmed_list`` называет происхождение, а не заслугу, и
**не** начинается с ``owner``: в этой кодовой базе ``owner`` — владелец САЛОНА
(``me.is_owner``), и такое имя приписало бы подтверждение не тому человеку.

Что команда считает, но не трогает
-----------------------------------

Число кандидатов печатается **до** изменения и обязано быть ровно 36. Не
совпало — останов, а не «применим сколько нашлось»: несовпадение значит, что
данные уехали с 25.09, и тогда пересматривать надо список, а не додавливать
команду. Пропавшие и уже поставленные строки называются **поимённо**: молча
меньшее число неотличимо от «столько и было».

Чего команда НЕ делает
----------------------

* **Ничего не создаёт и не удаляет.** Только ``template_id`` и
  ``mapping_status`` (плюс поля провенанса, без которых схема ``VERIFIED`` не
  примет). Имя, цена, длительность, активность — салонные.
* **Не трогает девять неподтверждённых.** Они остаются ``unmapped``: пустая
  связь честнее неверной. Три из них в каноне отсутствуют вовсе — дыра канона,
  §77 п.54, отдельный путь через заявку и верификацию.
* **Не пересопоставляет.** Список закрыт. Кода, который «улучшает» список,
  здесь нет и быть не должно.
* **Не оставляет синонима.** Админка на переводе в ``VERIFIED`` записывает
  салонную формулировку подтверждённым синонимом (§93, шаг 4,
  ``services/admin.py`` → ``_record_salon_wording_as_synonym``); запись через
  ``update()`` мимо админки этого не делает. Тот же размен назван у соседней
  ``confirm_seeded_links``; здесь он повторяется на боевом салоне, и следующий
  читатель должен знать: 36 салонных формулировок пилота подтверждёнными
  синонимами не станут, и резолвер их потом не найдёт. Метод переиспользуем
  вне админки (его зовёт ``mapping_review``), так что это ВЫБОР объёма листа,
  а не неизбежность — добавлять его молча на боевых данных я не стал.
* **Не переигрывает уже решённое.** Строка в терминальном статусе
  (``VERIFIED`` или ``NOT_RECOMMENDABLE``) не трогается: про неё уже сказали
  человек или другое правило. ``mapping_review`` держит это инвариантом, и
  оператору через админку такое недоступно; обойти его командой на боевом
  салоне значило бы стереть чужое решение вместе с его автором. Такие строки
  печатаются поимённо с указанием, кто решил.
* **Не выбирает между двойниками.** Если у названия из списка в салоне
  больше одной строки, команда останавливается: какую из них подтверждал
  владелец — ей неизвестно, а число кандидатов при этом сходится, и ворота
  на числе такой промах не ловят.
* **Не ставит ``VERIFIED`` на черновой канон.** Шаблон не в ``approved``
  пропускается и называется: админский путь такое прямо отказывает
  («сначала одобрить канон»), а ``check_canon_invariants`` считает
  ``verified_on_provisional`` размером дыры — расширять её командой нельзя.

Сухой прогон — умолчание
------------------------

Без ``--apply`` печатается область, время снятия, числа и построчное «было →
стало», не записывая ничего. Прямой ``UPDATE`` в обход команды оставил бы
провенанс пустым или выдуманным — то есть записал бы «подтверждено» без
подтвердившего.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from services.owner_confirmed_mapping import (
    CONFIRMED,
    OWNER_LIST_RULE,
    OWNER_LIST_RULE_VERSION,
    OWNER_LIST_SOURCE_REF,
    EXPECTED_BY_SALON,
)

#: Сколько строк обязано найтись. Не `len(CONFIRMED)`: число названо владельцем
#: и в документе, и сверять надо с НИМ, а не с длиной того же списка, который
#: мы же и применяем. Иначе укоротившийся список сам себя и оправдает.
EXPECTED = 36

#: Состояния, про которые УЖЕ РЕШЕНО. Берутся у `mapping_review`, а не
#: перечисляются здесь: две копии одного списка расходятся молча, и
#: добавивший состояние в одном месте не узнает, что второе продолжает
#: переигрывать чужие решения.
try:  # pragma: no cover - подстраховка на случай переноса модуля
    from services.mapping_review import TERMINAL as TERMINAL_STATUSES
except ImportError:  # pragma: no cover
    TERMINAL_STATUSES = frozenset({"verified", "not_recommendable"})


def _code_of(row, templates) -> str:
    """Код прежнего шаблона строки — для строки «было».

    UUID в сухом прогоне нечитаем ровно там, где решение оператора и
    требуется: при перенаправлении связи с чужого шаблона.
    """
    if row.template_id is None:
        return ""
    for code, tpl in templates.items():
        if tpl.pk == row.template_id:
            return code
    return str(row.template_id)


class Command(BaseCommand):
    help = (
        "Поставить 36 связей по списку, подтверждённому владельцем продукта "
        "25.09. По умолчанию — сухой прогон; запись только с --apply."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply", action="store_true",
            help="записать (по умолчанию — только показать, что было бы сделано)",
        )

    def handle(self, *args, **options) -> None:
        from django.conf import settings
        from django.db import connection

        from services.models import SalonService, ServiceTemplate
        from tenants.models import Tenant

        w = self.stdout.write
        stamped_at = timezone.now()

        if len(CONFIRMED) != EXPECTED:
            raise CommandError(
                f"список в коде содержит {len(CONFIRMED)} строк, а владелец "
                f"подтверждал {EXPECTED}. Это расхождение с основанием, а не "
                "повод применить сколько есть."
            )

        # Распределение по салонам сверяется с поправкой документа ДО базы:
        # список, съехавший на один салон, иначе дошёл бы до ворот и получил
        # бы ложный диагноз «данные уехали» вместо «посылка неверна».
        from collections import Counter

        actual = Counter(salon for salon, _, _ in CONFIRMED)
        if dict(actual) != EXPECTED_BY_SALON:
            raise CommandError(
                f"распределение по салонам {dict(actual)} не совпадает с "
                f"поправкой документа {EXPECTED_BY_SALON}. Это расхождение с "
                "основанием, а не повод применить как есть."
            )

        needed = sorted(actual)
        tenants: dict[str, object] = {}
        for slug in needed:
            tenant = Tenant.objects.filter(slug=slug).first()
            if tenant is None:
                # Молчаливый ноль неотличим от «нечего делать».
                raise CommandError(
                    f"салона со слагом «{slug}» нет: "
                    "список подтверждён именно для него"
                )
            tenants[slug] = tenant

        templates = {
            t.canonical_code: t
            for t in ServiceTemplate.objects.filter(
                canonical_code__in=[code for _, _, code in CONFIRMED]
            )
        }

        # Строки читаются по ПАРЕ (салон, имя): одно имя у двух салонов —
        # разные строки, и смешать их значит записать чужому салону.
        wanted_names = [name for _, name, _ in CONFIRMED]
        fetched = list(
            SalonService.objects.filter(
                tenant__in=list(tenants.values()), name__in=wanted_names
            ).order_by("pk")
        )
        rows: dict[tuple[object, str], SalonService] = {}
        duplicates: list[str] = []
        for r in fetched:
            key = (r.tenant_id, r.name)
            if key in rows:
                # Дубль по имени в пределах салона схема РАЗРЕШАЕТ: уникальна
                # только тройка (салон, шаблон, имя), а шаблон у целей пуст.
                # Словарь оставил бы произвольного двойника, число всё равно
                # сошлось бы, и второй остался бы непривязанным молча.
                duplicates.append(f"{r.tenant.slug} · {r.name}")
                continue
            rows[key] = r

        plan: list[tuple[SalonService, object, str]] = []
        missing_rows: list[str] = []
        missing_templates: list[str] = []
        not_approved: list[str] = []
        already: list[str] = []
        decided_by_someone: list[str] = []

        for salon, name, code in CONFIRMED:
            tenant = tenants[salon]
            row = rows.get((tenant.pk, name))
            template = templates.get(code)
            if row is None:
                missing_rows.append(f"{salon} · {name}")
                continue
            if template is None:
                missing_templates.append(f"{salon} · {name} → {code}")
                continue
            if template.lifecycle != ServiceTemplate.Lifecycle.APPROVED:
                # Админский путь такое прямо отказывает («сначала одобрить
                # канон»), и командой обходить этот отказ нельзя.
                not_approved.append(f"{salon} · {name} → {code} ({template.lifecycle})")
                continue
            if (
                row.template_id == template.pk
                and row.mapping_status == SalonService.MappingStatus.VERIFIED
                and row.mapping_confirmed_rule == OWNER_LIST_RULE
                and row.mapping_rule_version == OWNER_LIST_RULE_VERSION
            ):
                # Идемпотентность: строка уже доведена этим же правилом ТОЙ ЖЕ
                # версии. Иная версия — иное основание, и переписывать её
                # молча нельзя.
                already.append(f"{salon} · {name}")
                continue
            if row.mapping_status in TERMINAL_STATUSES:
                # Про строку уже решено — человеком или другим правилом.
                # `mapping_review` держит это как инвариант: «уже решено — не
                # переигрывается», и оператору через админку такое недоступно.
                # Командой обходить инвариант нельзя тем более: здесь боевой
                # салон, и чужое решение стёрлось бы вместе с его автором.
                who = (
                    f"человек #{row.mapping_confirmed_by_id}"
                    if row.mapping_confirmed_by_id
                    else f"правило {row.mapping_confirmed_rule or '—'}"
                    f" v{row.mapping_rule_version or '—'}"
                )
                decided_by_someone.append(
                    f"{salon} · {name}: {row.mapping_status}, {who}"
                )
                continue
            was = (
                f"template={_code_of(row, templates) or '—'} "
                f"status={row.mapping_status}"
            )
            plan.append((row, template, was))

        db = settings.DATABASES["default"]
        w(f"предмет: база {db.get('NAME')}@{db.get('HOST') or 'local'} · "
          f"vendor {connection.vendor}")
        w(f"снято:   {stamped_at.isoformat(timespec='seconds')}")
        area = ", ".join(f"{s}: {actual[s]}" for s in needed)
        w(f"область: {area} · только строки из списка владельца "
          f"({EXPECTED} имён)")
        w(f"режим:   {'ЗАПИСЬ (--apply)' if options['apply'] else 'сухой прогон, записи нет'}")
        w(f"правило: {OWNER_LIST_RULE} v{OWNER_LIST_RULE_VERSION}")
        w(f"основание: {OWNER_LIST_SOURCE_REF}")
        w("")

        found = len(plan) + len(already)
        w(f"кандидатов из списка найдено: {found} из {EXPECTED}")
        w(f"  к записи:            {len(plan)}")
        w(f"  уже поставлено:      {len(already)}")
        w(f"  строки салона нет:   {len(missing_rows)}")
        w(f"  шаблона канона нет:  {len(missing_templates)}")
        w(f"  канон не approved:   {len(not_approved)}")
        w(f"  уже решено другими:  {len(decided_by_someone)}")
        w(f"  дублей имени:        {len(duplicates)}")

        # Исключения называются ПОИМЁННО: меньшее число молча неотличимо от
        # «столько и было», и именно так список однажды и разойдётся с базой.
        # Для дублей и чужих решений число даже не меньше — оно ПРАВИЛЬНОЕ, а
        # запись ушла бы не туда; молчание здесь опаснее всего.
        for name in missing_rows:
            w(f"    нет строки салона: {name}")
        for pair in missing_templates:
            w(f"    нет шаблона канона: {pair}")
        for pair in not_approved:
            w(f"    канон не approved: {pair}")
        for item in decided_by_someone:
            w(f"    УЖЕ РЕШЕНО, не трогаю: {item}")
        for item in duplicates:
            w(f"    ДУБЛЬ ИМЕНИ, пропущен: {item}")

        # Дубль — НЕОДНОЗНАЧНОСТЬ, а не мелочь: владелец подтвердил «строку с
        # таким названием», и если таких две, команда не знает, какую именно.
        # Запись в первую попавшуюся была бы догадкой, а число при этом
        # сошлось бы — то есть ворота на числе такой промах НЕ ловят. Отсюда
        # отдельный останов: по тому же правилу, по которому девять
        # неподтверждённых остаются пустыми, пустая связь честнее неверной.
        if duplicates:
            raise CommandError(
                f"дублей имени: {len(duplicates)} — "
                + "; ".join(duplicates)
                + ". Останов: у названия из списка больше одной строки, и "
                "какую из них подтверждал владелец — команде неизвестно. "
                "Развести строки в салоне или уточнить список."
            )

        if found != EXPECTED:
            raise CommandError(
                f"кандидатов {found}, а владелец подтверждал {EXPECTED}. "
                "Останов: расхождение значит, что данные уехали с 25.09, и "
                "пересматривать надо список, а не додавливать команду."
            )

        w("")
        for row, template, was in plan:
            w(f"  {row.tenant.slug} · {row.name}")
            w(f"      было:  {was}")
            w(f"      стало: template={template.canonical_code} status=verified "
              f"rule={OWNER_LIST_RULE} v{OWNER_LIST_RULE_VERSION}")

        if not options["apply"]:
            w("")
            w("сухой прогон: ничего не записано. Запись — тем же вызовом с --apply.")
            return

        written = 0
        with transaction.atomic():
            for row, template, _ in plan:
                # Точечное обновление вместо `queryset.update()`: поля названы
                # поимённо, и ни одно салонное сюда не попадёт даже случайно.
                SalonService.objects.filter(pk=row.pk).update(
                    template=template,
                    mapping_status=SalonService.MappingStatus.VERIFIED,
                    mapping_confirmed_rule=OWNER_LIST_RULE,
                    mapping_rule_version=OWNER_LIST_RULE_VERSION,
                    mapping_confirmed_at=stamped_at,
                    mapping_source_ref=OWNER_LIST_SOURCE_REF,
                    # Подтверждает правило: «кто» снимается, иначе схема
                    # запретит оба заполненных поля.
                    mapping_confirmed_by=None,
                )
                written += 1

        w("")
        w(f"записано: {written}")
