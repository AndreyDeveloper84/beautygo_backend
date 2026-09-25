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
    OWNER_LIST_TENANT_SLUG,
)

#: Сколько строк обязано найтись. Не `len(CONFIRMED)`: число названо владельцем
#: и в документе, и сверять надо с НИМ, а не с длиной того же списка, который
#: мы же и применяем. Иначе укоротившийся список сам себя и оправдает.
EXPECTED = 36


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

        tenant = Tenant.objects.filter(slug=OWNER_LIST_TENANT_SLUG).first()
        if tenant is None:
            # Молчаливый ноль неотличим от «нечего делать».
            raise CommandError(
                f"салона со слагом «{OWNER_LIST_TENANT_SLUG}» нет: "
                "список подтверждён именно для него"
            )

        templates = {
            t.canonical_code: t
            for t in ServiceTemplate.objects.filter(
                canonical_code__in=[code for _, code in CONFIRMED]
            )
        }
        rows = {
            r.name: r
            for r in SalonService.objects.filter(
                tenant=tenant, name__in=[name for name, _ in CONFIRMED]
            )
        }

        plan: list[tuple[SalonService, object, str]] = []
        missing_rows: list[str] = []
        missing_templates: list[str] = []
        already: list[str] = []

        for name, code in CONFIRMED:
            row = rows.get(name)
            template = templates.get(code)
            if row is None:
                missing_rows.append(name)
                continue
            if template is None:
                missing_templates.append(f"{name} → {code}")
                continue
            if (
                row.template_id == template.pk
                and row.mapping_status == SalonService.MappingStatus.VERIFIED
                and row.mapping_confirmed_rule == OWNER_LIST_RULE
            ):
                # Идемпотентность: строка уже доведена этим же правилом.
                already.append(name)
                continue
            was = (
                f"template={row.template_id or '—'} status={row.mapping_status}"
            )
            plan.append((row, template, was))

        db = settings.DATABASES["default"]
        w(f"предмет: база {db.get('NAME')}@{db.get('HOST') or 'local'} · "
          f"vendor {connection.vendor}")
        w(f"снято:   {stamped_at.isoformat(timespec='seconds')}")
        w(f"область: салон {OWNER_LIST_TENANT_SLUG} · только строки из списка "
          f"владельца ({EXPECTED} имён)")
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

        # Исключения называются ПОИМЁННО: меньшее число молча неотличимо от
        # «столько и было», и именно так список однажды и разойдётся с базой.
        for name in missing_rows:
            w(f"    нет строки салона: {name}")
        for pair in missing_templates:
            w(f"    нет шаблона канона: {pair}")

        if found != EXPECTED:
            raise CommandError(
                f"кандидатов {found}, а владелец подтверждал {EXPECTED}. "
                "Останов: расхождение значит, что данные уехали с 25.09, и "
                "пересматривать надо список, а не додавливать команду."
            )

        w("")
        for row, template, was in plan:
            w(f"  {row.name}")
            w(f"      было:  {was}")
            w(f"      стало: template={template.pk} ({template.canonical_code}) "
              f"status=verified")

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
