"""``manage.py map_salon_services --tenant <slug> [--rules R1,R2] [--out <path>]`` — Level A, dry-run.

Печатает предмет (база, host, старт постмастера, число канонов с кодом,
**время снятия**), потом по строке на каждую услугу салона: решение,
причина, флаги, кандидаты с evidence, что стоит сейчас, план провенанса
(для ``AUTO_ELIGIBLE``), и сводку, посчитанную из тех же строк.

Срез задаётся ``--status`` и ``--source`` (DRF-2407). Без них считается весь
салон; со срезом печатаются **оба** числа — сколько строк у салона и сколько
в срезе, — потому что «44 не привязались» и «44 из 232 не привязались»
читаются по-разному, а решают по ним одно и то же.

**Ничего не пишет.** ``--apply`` принимается только чтобы ответить отказом
с названной причиной (``authorize_apply``): OD-NEW-7 не принят, запись —
MAP-AUTO-06. Правила по умолчанию выключены — без ``--rules`` каждый
кандидат-под-правило показывается как ``AUTO_NOT_ENABLED``.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from services.canonical_code import SEED_PATH
from services.mapping import ApplyNotAuthorized, RulesEnabled, resolve_tenant
from services.mapping.report import row_lines, summary_lines, write_json
from services.mapping.schema import SchemaNotReady, describe_subject
from services.mapping.store import store_report
from services.mapping.types import RULE_VERSION
from services.mapping_apply import ApplyStopped, apply_tenant, rollback_auto_rule
from tenants.models import Tenant


def _parse_slice(spec: str, allowed: set[str], what: str) -> set[str]:
    """Разобрать срез, отказав и на незнакомом значении, и на пустом наборе.

    Тихо пропустить опечатку нельзя: `--status unmaped` дал бы пустой срез, и
    ноль прочитался бы как «нечего привязывать». Ноль от написания и ноль от
    предмета — разные вещи, и отличить их постфактум по выводу невозможно.

    Зеркальный случай не менее опасен и сперва был упущен: `--status ","` или
    `--status "$ST"` с пустой переменной дают **ноль значений**, отбор не
    применяется вовсе — и весь салон печатается под заголовком среза. Тогда
    232 записывают в ответ на вопрос «сколько из 44». Поэтому непустая строка,
    не давшая ни одного значения, — тоже отказ.
    """

    values = {v.strip() for v in spec.split(",") if v.strip()}
    if spec.strip() and not values:
        raise ValueError(f"пустой срез по полю «{what}»: {spec!r} не называет ни одного значения")
    unknown = sorted(values - allowed)
    if unknown:
        raise ValueError(
            f"неизвестный {what}: {', '.join(unknown)}. Известные: {', '.join(sorted(allowed))}"
        )
    return values


def parse_slice_specs(status_spec: str, source_spec: str) -> tuple[set[str], set[str]]:
    """Разобрать оба среза разом — один вход для прогона, заголовка и JSON."""

    from services.models import SalonService

    statuses = _parse_slice(
        status_spec, {c for c, _ in SalonService.MappingStatus.choices}, "статус"
    )
    sources = _parse_slice(source_spec, {c for c, _ in SalonService.Source.choices}, "источник")
    return statuses, sources


def _slice(rows, tenant, statuses: set[str], sources: set[str]):
    """Сузить строки прогона до нужного среза.

    Сужение идёт ПОСЛЕ прогона и по идентификаторам: резолвер видит салон
    целиком и решает так же, как решал бы без среза. Иначе отчёт отвечал бы на
    вопрос «что будет, если в салоне только эти строки», а спрашивают другое.

    Статус берётся из самой строки прогона (`before.mapping_status`), а не из
    повторного запроса: иначе срез мог бы отобрать по одному статусу, а строка
    напечатать в «сейчас» другой. Источник на строке прогона не хранится —
    за ним приходится идти в базу.
    """

    from services.models import SalonService

    if not statuses and not sources:
        return rows

    if statuses:
        rows = [r for r in rows if r.before.mapping_status in statuses]
    if sources:
        keep = set(
            SalonService.objects.filter(tenant=tenant, source__in=sources).values_list(
                "pk", flat=True
            )
        )
        rows = [r for r in rows if r.salon_service_id in keep]
    return rows


def _slice_line(statuses: set[str], sources: set[str]) -> str:
    """Заголовок — из РАЗОБРАННЫХ наборов, а не из сырой строки.

    Иначе заголовок мог бы описывать отбор, которого не было: `--status ","`
    печаталось как «срез: статус ,» при несуженном салоне.
    """

    parts = []
    if statuses:
        parts.append(f"статус {','.join(sorted(statuses))}")
    if sources:
        parts.append(f"источник {','.join(sorted(sources))}")
    return " · ".join(parts) if parts else "весь салон"


class Command(BaseCommand):
    help = "Level A резолвер услуг салона → канон. Только dry-run; --apply отказывает с причиной."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="slug салона")
        parser.add_argument(
            "--rules", default="", help="включённые правила через запятую: R1,R2 (по умолчанию — ни одно)",
        )
        parser.add_argument("--out", default=None, help="куда записать JSON-отчёт (файл, не база)")
        parser.add_argument(
            "--status", default="",
            help=("срез по статусу связи через запятую: unmapped,review_required,verified,"
                  "not_recommendable (по умолчанию — весь салон)"),
        )
        parser.add_argument(
            "--source", default="",
            help="срез по источнику строки через запятую: yclients,seed,manual (по умолчанию — весь салон)",
        )
        parser.add_argument(
            "--dry-run", action="store_true", default=True, help="единственный режим; принимается для явности",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="записать AUTO_ELIGIBLE через форму §76 (MAP-AUTO-06); сегодня ворота закрыты — отказ OD-NEW-7",
        )
        parser.add_argument(
            "--rollback", action="store_true",
            help="снять записи правила данной --rule-version у салона (владельца не трогает); те же ворота",
        )
        parser.add_argument("--rule-version", default=RULE_VERSION, help="версия правил, на которой посчитан план")
        parser.add_argument("--seed-version", default=SEED_PATH.name, help="файл seed, на котором посчитан план")
        parser.add_argument("--only", default="", help="uuid услуг через запятую — применить только их")
        parser.add_argument("--per-row", action="store_true", help="каждая строка своей транзакцией")
        parser.add_argument(
            "--store", action="store_true",
            help=("сохранить отчёт как «последний прогон» салона "
                  "(файл MAPPING_REPORT_DIR/<slug>.json) — его читает админка"),
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(slug=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"салон «{options['tenant']}» не найден")
        try:
            rules = RulesEnabled.parse(options["rules"])
        except ValueError as exc:
            raise CommandError(str(exc))

        # Срез разбирается ДО ветки записи: иначе `--apply --status unmaped`
        # уходил бы в отказ ворот, ни разу не взглянув на опечатку.
        try:
            statuses, sources = parse_slice_specs(options["status"], options["source"])
        except ValueError as exc:
            raise CommandError(str(exc))

        if options["apply"] or options["rollback"]:
            if statuses or sources:
                # Сегодня ворота закрыты, и это latent. Но в день, когда они
                # откроются, `--apply --status unmapped` записал бы ВЕСЬ салон,
                # а оператор считал бы, что сузил до 44 строк. Для записи
                # сужение уже есть — `--only`, оно по идентификаторам.
                raise CommandError(
                    "срез (--status/--source) — только для сухого прогона; "
                    "для записи используйте --only с идентификаторами услуг"
                )
            return self._write(tenant, rules, options)

        report_ref = options["out"] or "dry-run"
        try:
            rows = resolve_tenant(tenant, rules, report_ref=report_ref)
        except SchemaNotReady as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            raise SystemExit(2)

        total_rows = len(rows)
        rows = _slice(rows, tenant, statuses, sources)

        w = self.stdout.write
        # Область и время — ДО чисел. Без них «0 привязалось» со стенда и с
        # эталонной базы выглядят одинаково, а означают разное.
        #
        # Предмет остаётся ПЕРВОЙ строкой: на это есть узел
        # (`test_command_prints_subject_first...`), и порядок двух строк шапки
        # того не стоит.
        w(describe_subject().line())
        w(f"снято: {timezone.now().isoformat(timespec='seconds')}")
        w(f"салон: {tenant.slug} ({tenant.pk}) · правила: "
          f"{', '.join(r for r in ('R0', 'R1', 'R2') if getattr(rules, r)) or 'ни одно (AUTO_NOT_ENABLED)'} "
          f"· версия правил: {RULE_VERSION} · режим: dry-run, записи нет")
        w(f"срез: {_slice_line(statuses, sources)} · "
          f"строк в срезе: {len(rows)} из {total_rows} у салона")
        w("")
        for r in rows:
            for line in row_lines(r):
                w(line)
        w("")
        for line in summary_lines(rows):
            w(line)
        if options["out"]:
            # Файл уходит дальше терминала, а «total: 44» без области читается
            # как весь салон — та же двусмысленность, ради снятия которой и
            # печатаются два числа.
            write_json(
                rows,
                Path(options["out"]),
                scope={
                    "tenant": tenant.slug,
                    "slice": _slice_line(statuses, sources),
                    "rows_in_slice": len(rows),
                    "rows_in_tenant": total_rows,
                },
            )
            w(f"JSON: {options['out']}")
        if options["store"]:
            if statuses or sources:
                # Отчёт админки — один файл на салон, и админка читает его как
                # полный: строка, которой в нём нет, показывается как «нет в
                # отчёте», действие «подтвердить кандидата» на неё отказывает,
                # а «отметить пробел канона» пишет провенанс с пометкой «без
                # отчёта». Срез, записанный сюда, увёл бы весь салон в это
                # состояние молча — и это ровно тот дефект, против которого
                # лист написан, только перенесённый с экрана в админку.
                raise CommandError(
                    "--store сохраняет отчёт на ВЕСЬ салон, а срез его сузил бы: "
                    "запустите --store без --status/--source"
                )
            path = store_report(rows, tenant.slug, rules=options["rules"] or "")
            w(f"отчёт последнего прогона (для админки): {path}")

    def _write(self, tenant, rules, options) -> None:
        """Apply / откат. Ворота — внутри (authorize_apply); сегодня всегда отказ."""
        w = self.stdout.write
        try:
            if options["rollback"]:
                rep = rollback_auto_rule(tenant, rule_version=options["rule_version"])
                w(f"откат {tenant.slug} v={options['rule_version']}: снято строк {len(rep.reverted)}, "
                  f"синонимов {rep.synonyms_removed}")
                return
            only = {s.strip() for s in options["only"].split(",") if s.strip()} or None
            rep = apply_tenant(
                tenant, rules, rule_version=options["rule_version"], seed_version=options["seed_version"],
                report_ref=options["out"] or "apply", only=only, per_row=options["per_row"],
            )
        except ApplyNotAuthorized as exc:
            raise CommandError(str(exc))
        except (ApplyStopped, SchemaNotReady) as exc:
            raise CommandError(f"apply остановлен, записей нет: {exc}")
        w(describe_subject().line())
        w(f"apply {tenant.slug}: план {rep.planned}, к записи {rep.eligible}, записано {rep.written}, "
          f"пропущено {rep.skipped}, провалено {rep.failed}")
        for r in rep.rows:
            w(f"  {r.outcome:<8} {r.name} — {r.detail}")
        w(f"рёбра мастер×услуга: до {dict(rep.edges_verdict_before)} → после {dict(rep.edges_verdict_after)}")
