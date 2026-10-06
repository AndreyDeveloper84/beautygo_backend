"""Счётчик для дыры, которую решено пока не закрывать (§93).

Зачем команда, а не гейт
------------------------

Сегодня связь со статусом `VERIFIED` на **черновом** каноне попадает в
подбор: гейт §76 спрашивает статус связи и ничего не знает про
`ServiceTemplate.lifecycle`. Правильно это или нет — вопрос владельцу, и
он упирается в два других его же незакрытых («кто оператор» и «что
значит после проверки»).

Заводить второй гейт по собственной догадке нельзя: он тише и опаснее
отсутствующего. Пустая полка, полученная правилом, которого никто не
просил, выглядит снаружи точно так же, как пустая полка по честной
причине, — и разобраться, почему клиент никого не увидел, будет негде.

Но и оставлять дыру молчаливой нельзя тоже. Разница между «мы решили
не закрывать» и «мы не заметили» — в том, есть ли число. Эта команда
и есть число.

Что считается
-------------

Три величины, и каждая отвечает на свой вопрос::

    verified_on_provisional  сколько связей УЖЕ прошли бы гейт через
                             непроверенный канон — размер дыры
    provisional_templates    очередь одобрения куратора
    approved_without_basis   положительная стража: ограничение схемы
                             живо, здесь обязан быть ноль
    verified_on_retired      Body Care CAT-2: сколько связей салонов всё ещё
                             VERIFIED на ВЫВЕДЕННОМ каноне. Подбор статус
                             канона не видит, и такая связь остаётся
                             рекомендуемой — пока допустимость не связана
                             со статусом (CAT-10). Нарушение.
    retired_without_basis    положительная стража для вывода из оборота —
                             та же роль, что у approved_without_basis
    candidate_templates      очередь кандидатов на активацию
    known_system_derived_facts  Body Care CAT-4: сколько ИЗВЕСТНЫХ фактов
                             конфигурации предложений взяты системой
                             (``source_type=SYSTEM_DERIVED``). По §18
                             runtime body-care факты не выдумывает, так что
                             число ожидается около нуля. НЕ нарушение и не
                             гейт — какие поля запрещают SYSTEM_DERIVED,
                             решает клиника (D-4); это канарейка: растущее
                             число значит «система изобретает факты».

Третья строка — не украшение. Первые две могут быть нулями оттого, что
запрос неверен, и тогда «дыры нет» читалось бы как хорошая новость.
Третья проверяет тем же способом то, что заведомо запрещено схемой:
если и она ноль, значит счётчик как минимум работает на данных.

Использование::

    python manage.py check_canon_invariants
    python manage.py check_canon_invariants --fail-on-violations   # для CI
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from services.models import OfferingConfigFact, SalonService, ServiceTemplate


class Command(BaseCommand):
    help = "Посчитать нарушения инвариантов канонического слоя (§93)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--fail-on-violations",
            action="store_true",
            help="Ненулевой код выхода, если найдено хоть одно нарушение.",
        )

    def handle(self, *args, **options) -> None:
        # `all_tenants` не нужен: обе модели глобальные, тенантного
        # менеджера у них нет. Если это изменится, счёт молча схлопнется
        # в ноль по одному салону — и строка «предмет» ниже это покажет.
        verified_on_provisional = SalonService.objects.filter(
            mapping_status=SalonService.MappingStatus.VERIFIED,
            template__lifecycle=ServiceTemplate.Lifecycle.PROVISIONAL,
        ).count()

        provisional_templates = ServiceTemplate.objects.filter(
            lifecycle=ServiceTemplate.Lifecycle.PROVISIONAL,
        ).count()

        approved_without_basis = ServiceTemplate.objects.filter(
            lifecycle=ServiceTemplate.Lifecycle.APPROVED,
            approval_source_ref="",
        ).count()

        # Body Care CAT-2. Выведенный канон со связями VERIFIED — дыра, а не
        # вопрос: подбор (`mapping_status == VERIFIED`) статуса канона не
        # видит, и связь остаётся рекомендуемой. Закрывает её CAT-10.
        verified_on_retired = SalonService.objects.filter(
            mapping_status=SalonService.MappingStatus.VERIFIED,
            template__lifecycle=ServiceTemplate.Lifecycle.RETIRED,
        ).count()

        retired_without_basis = ServiceTemplate.objects.filter(
            lifecycle=ServiceTemplate.Lifecycle.RETIRED,
            retirement_source_ref="",
        ).count()

        candidate_templates = ServiceTemplate.objects.filter(
            lifecycle=ServiceTemplate.Lifecycle.CANDIDATE,
        ).count()

        # Body Care CAT-4: канарейка, не нарушение — в `violations` не входит.
        known_system_derived_facts = OfferingConfigFact.objects.filter(
            state=OfferingConfigFact.State.KNOWN,
            source_type=OfferingConfigFact.SourceType.SYSTEM_DERIVED,
        ).count()

        # MAP-AUTO-01: сколько канонов без логической идентичности. Не
        # нарушение само по себе — 40 строк DRF-196 и PROVISIONAL-каноны
        # кода не имеют законно; число нужно, чтобы видеть, дошёл ли
        # bootstrap 0024 до этой базы (после него у seed-строк код есть).
        templates_without_canonical_code = ServiceTemplate.objects.filter(
            canonical_code__isnull=True,
        ).count()

        # Предмет печатается рядом с результатом: без него «ноль»
        # неотличим от «посчитали не то». Всего строк — чтобы было
        # видно, что счётчик смотрел на непустую таблицу.
        self.stdout.write(
            f"предмет: {timezone.now().isoformat(timespec='seconds')} · "
            f"SalonService={SalonService.objects.count()} · "
            f"ServiceTemplate={ServiceTemplate.objects.count()}"
        )
        self.stdout.write(f"verified_on_provisional : {verified_on_provisional}")
        self.stdout.write(f"provisional_templates   : {provisional_templates}")
        self.stdout.write(f"approved_without_basis  : {approved_without_basis}")
        self.stdout.write(f"verified_on_retired     : {verified_on_retired}")
        self.stdout.write(f"retired_without_basis   : {retired_without_basis}")
        self.stdout.write(f"candidate_templates     : {candidate_templates}")
        self.stdout.write(f"known_system_derived_facts : {known_system_derived_facts}")
        self.stdout.write(f"templates_without_canonical_code : {templates_without_canonical_code}")

        violations = (
            verified_on_provisional
            + approved_without_basis
            + verified_on_retired
            + retired_without_basis
        )
        if violations:
            self.stdout.write(self.style.WARNING(
                f"нарушений: {violations}. `verified_on_provisional` — не поломка, "
                "а незакрытый вопрос владельцу; `verified_on_retired` — выведенный "
                "канон всё ещё в подборе через VERIFIED-связи (до CAT-10); "
                "`approved_without_basis` и `retired_without_basis` — поломка "
                "схемы, ограничение обойдено `update()`."
            ))
            if options["fail_on_violations"]:
                raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS("нарушений не найдено"))
