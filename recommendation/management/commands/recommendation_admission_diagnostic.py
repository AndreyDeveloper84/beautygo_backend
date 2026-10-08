"""``manage.py recommendation_admission_diagnostic [--tenant <slug> ...] [--goal <key> | --text <слова>] [--details]``

Диагностический прогон подбора: что закрывает каждая проверка допуска
каталога — без проверок, с каждой отдельно и со всеми вместе (DRF-2888).

Решение владельца 07.10: прежде чем включать правило «неизвестное не
допускается», подбор, ранжирование и цели проверяются в изолированном
диагностическом режиме, а список проверок резолвера сверяется с dry-run
каталога (``body_care_admission_dry_run``).

**Только чтение.** Команда ничего не пишет, флаги на стенде не трогает,
ничего не отправляет. Её результат — отчёт, а не ответ границы: записаться
по нему нечем. На проводе такого режима нет: ручка ``resolve`` политику
стадий не принимает и набор проверок берёт полный.

Что именно отключается
----------------------
Только восемь проверок допуска каталога (``AdmissionCheck``): связь, канон
выведен, область, конфигурация, юр. класс, лицензия, адрес, квалификация.
Безопасность хода, «предложение неактивно / мастер не умеет», совпадение с
нуждой, бюджет, область запроса и видимость демо-салонов действуют во всех
прогонах одинаково — они не допуск каталога.

Что печатается
--------------
* область замера — первой: время, база, салоны, нужда, положение флага
  ``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED``;
* матрица «проверка × исход» по всем кандидатам области: сколько кандидатов
  каждая проверка пропускает, закрывает, к скольким не относится и у
  скольких **не действует** (едут на обходе, пока флаг выключен);
* прогоны настоящего резолвера: без проверок, с каждой проверкой отдельно,
  со всеми. У каждого прогона первой строкой слово «ДИАГНОСТИКА» и набор
  включённых проверок; затем — сколько кандидатов в выдаче, сколько
  исключено и какими кодами;
* с ``--details`` — построчно по кандидатам: ответы всех восьми проверок и
  место в выдаче каждого прогона.

Чего команда не делает
----------------------
* **Не печатает людей**: кандидат — идентификатор, без имени.
* **Не толкует**: принять ли последствия включения флага — решение владельца.
* Не знает про гейт новой записи и про то, что бот скажет человеку.
* Личность спрашивающего — обычный клиент: демо-салоны скрыты, как у него.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from recommendation.api import (
    ALL_CHECKS,
    AdmissionCheck,
    CheckOutcome,
    NeedOrigin,
    NeedSpec,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    StagePolicy,
    Surface,
    resolve,
)

FLAG = "BODY_CARE_UNCLASSIFIED_FAIL_CLOSED"

TITLES = {
    AdmissionCheck.MAPPING: "связь с каноном подтверждена",
    AdmissionCheck.CANON_RETIRED: "канон не выведен из оборота",
    AdmissionCheck.SCOPE: "область классификации известна",
    AdmissionCheck.CONFIG: "конфигурация Body Care готова",
    AdmissionCheck.LEGAL_CLASS: "юридический класс подтверждён",
    AdmissionCheck.LICENSE: "лицензия салона",
    AdmissionCheck.ADDRESS: "адрес мастера",
    AdmissionCheck.QUALIFICATION: "квалификация мастера",
}

OUTCOMES = (
    (CheckOutcome.PASSED, "пройдена"),
    (CheckOutcome.FAILED, "не пройдена"),
    (CheckOutcome.UNDETERMINED, "не определено"),
    (CheckOutcome.NOT_ENFORCED, "НЕ ДЕЙСТВУЕТ"),
    (CheckOutcome.NOT_APPLICABLE, "не относится"),
)

HEADER = "ДИАГНОСТИКА"


def runs() -> list[tuple[str, frozenset[AdmissionCheck]]]:
    """Наборы проверок: ни одной, каждая по одной, все. Порядок — порядок отчёта."""
    return [
        ("без проверок допуска", frozenset()),
        *((f"только «{TITLES[check]}»", frozenset({check})) for check in ALL_CHECKS),
        ("все проверки (как на проводе)", frozenset(ALL_CHECKS)),
    ]


class Command(BaseCommand):
    help = "Диагностический прогон подбора: что закрывает каждая проверка допуска (DRF-2888). Только чтение."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--tenant", action="append", default=[], metavar="SLUG",
                            help="сузить область до салона; можно повторять. Без него — весь каталог")
        need = parser.add_mutually_exclusive_group()
        need.add_argument("--goal", default=None, metavar="KEY", help="ключ цели — нужда «по цели»")
        need.add_argument("--text", default=None, metavar="СЛОВА", help="слова клиента — названная нужда")
        parser.add_argument("--k", type=int, default=50, help="сколько мест выдачи считать (по умолчанию 50)")
        parser.add_argument("--details", action="store_true", help="построчно по кандидатам")

    def handle(self, *args: Any, **options: Any) -> None:
        # Импорты домена — внутри: модуль команды не должен тянуть модели при импорте пакета.
        from tenants.models import Tenant
        from users.recommendation_source import SpecialistCandidateSource

        slugs = list(dict.fromkeys(options["tenant"]))
        tenants = dict(Tenant.objects.filter(slug__in=slugs).values_list("slug", "id")) if slugs else {}
        unknown = sorted(set(slugs) - set(tenants))
        if unknown:
            raise CommandError(f"нет салона со слагом: {', '.join(unknown)}")

        if options["goal"]:
            need = NeedSpec(origin=NeedOrigin.GOAL, goal_key=options["goal"])
            need_label = f"цель «{options['goal']}»"
        elif options["text"]:
            need = NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text=options["text"])
            need_label = f"слова «{options['text']}»"
        else:
            need = NeedSpec(origin=NeedOrigin.MEMORY)
            need_label = "не названа"
        scope = Scope(ScopeMode.MARKETPLACE, tenant_refs=tuple(tenants.values()))
        source = SpecialistCandidateSource()

        write = self.stdout.write
        write(f"{HEADER} · допуск каталога в подборе · только чтение, ничего не записано и не отправлено")
        write(f"время: {timezone.now().isoformat(timespec='seconds')}")
        write(f"база: {connection.settings_dict.get('NAME')} @ {connection.settings_dict.get('HOST') or 'local'}")
        write(f"салоны: {', '.join(slugs) if slugs else 'весь каталог'}")
        write(f"нужда: {need_label}")
        write(f"флаг {FLAG}: {'ВКЛЮЧЁН' if getattr(settings, FLAG, False) else 'выключен'}")
        write("личность спрашивающего: обычный клиент (демо-салоны скрыты)")
        write("")

        candidates = list(source.fetch(scope=scope, need=need))
        write(f"кандидатов в области: {len(candidates)}")
        self._matrix(candidates)

        placements: dict[str, dict[Any, int]] = {}
        for title, checks in runs():
            decision = resolve(
                RecommendationRequest(
                    request_id="admission-diagnostic", subject_ref="diagnostic",
                    surface=Surface.MINIAPP_HOME, scope=scope, need=need,
                    safety_state=SafetyState.NOT_APPLICABLE, tie_break_seed="admission-diagnostic",
                    k=options["k"],
                ),
                source=source,
                policy=StagePolicy(admission_checks=checks),
            )
            placements[title] = {c.candidate_ref.id: c.rank for c in decision.ordered}
            reasons = Counter(e.reason_code.value for e in decision.excluded)
            write("")
            write(f"{HEADER} · прогон: {title}")
            write(f"  включены: {', '.join(c.value for c in ALL_CHECKS if c in checks) or '— ни одной —'}")
            write(f"  в выдаче: {len(decision.ordered)} · исключено: {len(decision.excluded)}")
            for code, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
                write(f"    {count:>4}  {code}")

        if options["details"]:
            self._details(candidates, placements)

    def _matrix(self, candidates) -> None:
        write = self.stdout.write
        write("")
        write(f"{HEADER} · проверка × исход (по отвечающей строке каждого кандидата)")
        legacy = sum(1 for facts in candidates if facts.admission is None)
        if legacy:
            write(f"  без канонической связи (судит только статус связи): {legacy}")
        write("  " + "проверка".ljust(34) + "".join(label.rjust(15) for _, label in OUTCOMES))
        for check in ALL_CHECKS:
            tally = Counter(
                answer.outcome
                for facts in candidates if facts.admission is not None
                for answer in facts.admission if answer.check is check
            )
            write("  " + TITLES[check].ljust(34) + "".join(str(tally.get(o, 0)).rjust(15) for o, _ in OUTCOMES))

    def _details(self, candidates, placements) -> None:
        write = self.stdout.write
        write("")
        write(f"{HEADER} · по кандидатам (идентификатор, ответы восьми проверок, место в каждом прогоне)")
        for facts in sorted(candidates, key=lambda f: str(f.ref.id)):
            write(f"  {facts.ref.id}")
            if facts.admission is None:
                write(f"      без канонической связи; статус связи: {facts.mapping_status.value}")
            else:
                for answer in facts.admission:
                    reason = f" · {answer.reason.value}" if answer.reason is not None else ""
                    write(f"      {answer.check.value:<14} {answer.outcome.value:<15}{reason}")
            ranks = ", ".join(
                f"{title}: {ranks.get(facts.ref.id, '—')}" for title, ranks in placements.items()
            )
            write(f"      место: {ranks}")
