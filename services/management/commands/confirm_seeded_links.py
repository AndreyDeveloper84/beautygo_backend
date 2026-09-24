"""``manage.py confirm_seeded_links [--apply]`` — довести связь строк сида (DRF-2408).

Решение владельца §77 п.31 (24.09): демонстрационные салоны остаются, и у их
услуг должна быть **нормальная связь с каноном**. Привязка у них уже стоит —
не хватает **статуса и провенанса**.

Что команда делает
------------------

Ставит ``VERIFIED`` строкам, у которых:

* источник ``seed``;
* привязка к канону **есть** (``template`` не пуст);
* статус ``review_required``.

Провенанс — **своё** имя правила, своя версия, дата; ``mapping_confirmed_by``
остаётся пустым: подтверждает правило, а не человек.

Почему своё имя, а не правило выбора мастера
--------------------------------------------

Строки выбора мастера узнаются по ``mapping_source_ref`` вида
``master_select:*`` (DRF-2406), а у строк сида основание **пусто**. Подтвердить
их тем правилом значило бы написать «мастер выбрал» там, где мастер не выбирал,
— подлог провенанса. Провенанс потом читают как факт, поэтому такая ошибка
дороже незакрытой очереди.

Имя правила называет **происхождение**, а не заслугу: связь пришла из
демонстрационного набора, и через год это должно читаться именно так.

Чего команда НЕ делает
----------------------

* **Ничего не удаляет и не создаёт.** Только статус и провенанс у существующих
  строк.
* **Не трогает защищённые слаги.** ``formula-tela`` несёт настоящие записи
  настоящих людей; ту же защиту держит сид демонстрационных салонов.
* **Не подтверждает строку без привязки.** Подтверждать нечего, и схема такую
  строку всё равно не примет. Такие строки **считаются отдельно** и печатаются
  числом: замер «у всех привязка есть» сделан не здесь, и если на стенде
  найдётся исключение, оно должно быть **названо**, а не пройти молча.
* **Не ходит в ворота резолвера.** ``authorize_apply`` охраняет запись решений
  резолвера (MAP-AUTO-06): выбор кандидата, план провенанса, правила R0/R1/R2.
  Здесь кандидата не ищут — привязка уже стоит, — поэтому предмет ворот не
  затрагивается. Это проверено запуском, а не рассуждением: узел
  ``test_the_resolver_gate_is_not_in_the_way`` ставит рядом отказ ворот на
  ``map_salon_services --apply`` и успешную запись этой команды.

Сухой прогон — умолчание
------------------------

Без ``--apply`` команда только печатает, что сделала бы. Прямой ``UPDATE`` в
обход команды оставил бы провенанс пустым или выдуманным — то есть записал бы
«подтверждено» без подтвердившего.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

#: Имя правила: называет ПРОИСХОЖДЕНИЕ связи, а не заслугу. «Проверено» здесь
#: было бы неправдой — никто ничего не проверял; связь пришла из набора
#: демонстрационных салонов, и провенанс должен говорить это.
SEED_LINK_RULE = "seeded_demo_catalog_link"
#: Версия правила. Меняется, когда меняется само основание подтверждения.
SEED_LINK_RULE_VERSION = "1"
#: Основание: чем именно заведена связь. Файл сида датирован, по нему видно,
#: каким изданием набора строка появилась.
SEED_LINK_SOURCE_REF = "seed_demo_salons"

#: Слаги, в которые команда не пишет никогда. Тот же список, что у сида
#: демонстрационных салонов: пилотный тенант несёт настоящие записи настоящих
#: людей, и правило про демонстрационный набор к ним отношения не имеет.
PROTECTED_SLUGS = frozenset({"formula-tela"})


class Command(BaseCommand):
    help = (
        "Довести связь строк сида: VERIFIED со своим правилом и провенансом. "
        "По умолчанию — сухой прогон; запись только с --apply."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply", action="store_true",
            help="записать (по умолчанию — только показать, что было бы сделано)",
        )
        parser.add_argument(
            "--tenant", default="",
            help="сузить до одного салона по слагу (по умолчанию — все демонстрационные)",
        )

    def handle(self, *args, **options) -> None:
        from django.conf import settings
        from django.db import connection

        from services.models import SalonService

        slug = options["tenant"].strip()
        if slug in PROTECTED_SLUGS:
            raise CommandError(
                f"салон «{slug}» защищён: он несёт настоящие записи настоящих людей, "
                "и правило про демонстрационный набор к нему не применяется"
            )

        rows = SalonService.objects.filter(
            source=SalonService.Source.SEED,
            mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
        ).exclude(tenant__slug__in=PROTECTED_SLUGS)
        if slug:
            rows = rows.filter(tenant__slug=slug)

        linked = rows.filter(template__isnull=False)
        # Строки без привязки считаются отдельно и называются числом. Замер «у
        # всех привязка есть» сделан не здесь; если на стенде найдётся
        # исключение, оно должно быть видно, а не пройти молча.
        unlinked = rows.filter(template__isnull=True).count()

        db = settings.DATABASES["default"]
        w = self.stdout.write
        w(f"предмет: база {db.get('NAME')}@{db.get('HOST') or 'local'} · "
          f"vendor {connection.vendor}")
        w(f"снято:   {timezone.now().isoformat(timespec='seconds')}")
        w(f"область: источник seed · статус review_required · "
          f"салон {slug or 'все, кроме защищённых'}")
        w(f"режим:   {'ЗАПИСЬ (--apply)' if options['apply'] else 'сухой прогон, записи нет'}")
        w(f"правило: {SEED_LINK_RULE} v{SEED_LINK_RULE_VERSION}")
        w("")

        by_tenant: dict[str, int] = {}
        for tenant_slug, count in (
            linked.values_list("tenant__slug").order_by().annotate(n=_count()).values_list(
                "tenant__slug", "n"
            )
        ):
            by_tenant[tenant_slug] = count

        total = sum(by_tenant.values())
        for tenant_slug in sorted(by_tenant):
            w(f"  {tenant_slug:<20} {by_tenant[tenant_slug]:>4}")
        w("")
        w(f"с привязкой (к записи):   {total}")
        w(f"без привязки (пропущено): {unlinked}")

        if not options["apply"]:
            w("")
            w("сухой прогон: ничего не записано. Запись — тем же вызовом с --apply.")
            return

        written = linked.update(
            mapping_status=SalonService.MappingStatus.VERIFIED,
            mapping_confirmed_rule=SEED_LINK_RULE,
            mapping_rule_version=SEED_LINK_RULE_VERSION,
            mapping_confirmed_at=timezone.now(),
            mapping_source_ref=SEED_LINK_SOURCE_REF,
            # Человек в провенансе снимается: подтверждает правило. Оба
            # заполненных поля схема запрещает, и наполовину заполненная строка
            # уронила бы прогон на середине.
            mapping_confirmed_by=None,
        )
        w("")
        w(f"записано: {written}")


def _count():
    from django.db.models import Count

    return Count("id")
