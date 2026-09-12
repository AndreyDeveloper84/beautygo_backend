"""``manage.py verify_pilot_slice --by <username> [--apply] [--include-disputed]``

Четырнадцать действий владельца из
``docs/OWNER_TASK_MAPPING_PILOT_SLICE_2026-09-12.md`` (§3 списка E1) —
одной командой вместо четырнадцати сохранений в админке. Каждое действие
одно и то же: **услуга formula-tela по имени → канон по коду из
справочника 1223 → ``mapping_status = VERIFIED`` с провенансом §76**.

Что команда НЕ решает
---------------------

Она исполняет решение владельца, а не принимает его: без ``--apply``
ничего не пишется, и даже с ``--apply`` пять строк, по которым в §4 списка
есть открытый вопрос владельцу, пропускаются с названной причиной — пока
он не сказал ``--include-disputed``. «Apply» — это его слово, как с
фикстурами.

Предмет рядом с результатом
---------------------------

Первым печатается, **над чем** команда работает: база, салон (slug и id),
кто подтверждает, сколько строк в таблице и в каком режиме. Без этого
«14 строк, 0 изменений» на не той базе прочиталось бы как «всё уже
сделано». Дальше — по строке на действие: найдена ли услуга, найден ли
канон по коду, что у него ``requires_health_check`` и совпал ли он с
ожиданием таблицы, что стоит сейчас и что станет.

Почему код канона резолвится через seed-файл
--------------------------------------------

У ``ServiceTemplate`` **нет поля с кодом**: ``1.3.24`` живёт только в
``services/seeds/canonical_catalog_2026-07.json``, а строка справочника
опознаётся парой (подкатегория, название) — так её пишет
``seed_canonical_catalog``. Поэтому команда берёт код → пару из seed'а →
строку в базе, и печатает оба шага: «код не найден в seed» и «пара не
найдена в базе» — разные новости.

Почему провенанс — «кто», а не «правило»
----------------------------------------

Форма §76 (``SalonServiceAdminForm``) держит «кто» и «правило»
взаимоисключающими: происхождение одно. Здесь решает **человек**
(``--by``), поэтому ``mapping_confirmed_by`` = он, а строка задания и
версия листа уезжают в ``mapping_source_ref`` — основание, которое форма
тоже требует. ``mapping_confirmed_rule`` / ``mapping_rule_version``
остаются пустыми намеренно.

Запись идёт через ту же форму, что и админка, а после сохранения — тот же
шаг 4 §93 (название салона как подтверждённый синоним канона), которым
админка заканчивает своё сохранение. Повторный прогон ничего не меняет:
уже ``VERIFIED`` с тем же каноном — «сделано», пропуск.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.forms.models import model_to_dict
from django.utils import timezone

from services.admin import SalonServiceAdmin, SalonServiceAdminForm
from services.models import SalonService, ServiceTemplate
from services.normalization import normalize_service_name
from tenants.models import Tenant

SEED = Path(__file__).resolve().parents[2] / "seeds" / "canonical_catalog_2026-07.json"
TASK_DOC = "docs/OWNER_TASK_MAPPING_PILOT_SLICE_2026-09-12.md"
LIST_VERSION = "pilot-slice-2026-09-12"
DEFAULT_TENANT = "formula-tela"


@dataclass(frozen=True)
class Action:
    n: int
    service_name: str      #: как в матрице 09.08 и в задании владельца — дословно
    code: str              #: код канона из справочника 1223
    expected_rhc: bool     #: колонка RHC задания; расхождение с базой — стоп по строке
    slice_row: str         #: строка §7 сценариев
    disputed: str = ""     #: пункт §4 списка, без ответа на который строка не исполняется


#: Таблица задания владельца — §3 ``PILOT_SLICE_MAPPING_LIST_2026-09-12.md``,
#: дословно. Менять здесь = менять задание; поэтому тест сверяет коды и
#: RHC с seed'ом, а число строк — с документом.
ACTIONS: tuple[Action, ...] = (
    Action(1, "Массаж шейно-воротниковой зоны", "1.1.5", False, "3"),
    Action(2, "Классический массаж", "1.1.1", False, "3"),
    Action(3, "Массаж спины — глубокая проработка (45 минут)", "1.1.4", False, "7, 9"),
    Action(4, "Массаж спины премиум — максимальная проработка (60 минут)", "1.1.4", False, "7"),
    Action(5, "Массаж спины — снятие боли и зажимов (30 минут)", "1.3.24", True, "7 (S3)",
           disputed="§4.3: 1.3.24 (requires_health_check=true) — намеренно, ради S3"),
    Action(6, "Лимфодренажный массаж — снятие отёков (экспресс 30 минут)", "1.4.1", False, "2",
           disputed="§4.4: строка 2 до safety-матрицы (B6 CLARIFY)"),
    Action(7, "Лимфодренажный массаж всего тела (60 минут)", "1.4.1", False, "2",
           disputed="§4.4: строка 2 до safety-матрицы (B6 CLARIFY)"),
    Action(8, "Массаж ног — снятие усталости и отёков (30 минут)", "1.4.18", False, "2 (ноги)",
           disputed="§4.4: строка 2 до safety-матрицы (B6 CLARIFY)"),
    Action(9, "Массаж ног — глубокое расслабление и лимфодренаж (45 минут)", "1.4.2", False, "2 (ноги)",
           disputed="§4.4: строка 2 до safety-матрицы (B6 CLARIFY)"),
    Action(10, "Массаж лица пластический / скульптурный", "1.5.13", False, "1"),
    Action(11, "Альгинатная маска", "4.4.1", False, "1"),
    Action(12, "Карбокситерапия", "4.5.1", False, "1"),
    Action(13, "Комбинированная чистка лица", "4.2.4", False, "1"),
    Action(14, "УЗ-чистка лица", "4.2.3", False, "1"),
)


def load_seed(path: Path = SEED) -> dict[str, dict]:
    return {row["code"]: row for row in json.loads(path.read_text(encoding="utf-8"))}


def template_for_code(code: str, seed: dict[str, dict]) -> tuple[ServiceTemplate | None, str]:
    """Канон по коду: seed даёт пару (подкатегория, название), база — строку.

    Возвращает (шаблон | None, причина). Причины различают «кода нет в
    seed», «пары нет в базе» и «пара неоднозначна» — это разные действия
    для владельца.
    """
    row = seed.get(code)
    if row is None:
        return None, f"код {code} не найден в {SEED.name}"
    category_name = row.get("subcategory") or row["category"]
    found = list(ServiceTemplate.objects.filter(category__name=category_name, name=row["service"]))
    if not found:
        return None, f"канон «{row['service']}» ({category_name}) по коду {code} не найден в базе"
    if len(found) > 1:
        return None, f"канон по коду {code} неоднозначен в базе: {len(found)} строк"
    return found[0], ""


def salon_service_by_name(tenant: Tenant, name: str) -> tuple[SalonService | None, str]:
    """Услуга салона по названию — по нормализованному ключу, а не по байтам.

    Названия в задании — из матрицы 09.08 с её двойными пробелами и «ё»;
    ``normalize_service_name`` — тот же ключ, которым сравнивает синонимы.
    """
    key = normalize_service_name(name)
    matches = [s for s in SalonService.objects.filter(tenant=tenant) if normalize_service_name(s.name) == key]
    if not matches:
        return None, "услуга не найдена у салона"
    if len(matches) > 1:
        return None, f"услуга неоднозначна: {len(matches)} строк с таким названием"
    return matches[0], ""


class Command(BaseCommand):
    help = "Исполнить 14 действий владельца по маппингу pilot slice (formula-tela). Без --apply — только план."

    def add_arguments(self, parser):
        parser.add_argument("--by", required=True, help="username того, кто подтверждает (mapping_confirmed_by).")
        parser.add_argument("--tenant", default=DEFAULT_TENANT, help=f"slug салона (по умолчанию {DEFAULT_TENANT}).")
        parser.add_argument("--apply", action="store_true", help="Писать. Без флага — сухой прогон.")
        parser.add_argument(
            "--include-disputed", action="store_true",
            help="Исполнять и строки с открытым вопросом §4 (по умолчанию пропускаются с причиной).",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            who = User.objects.get(username=options["by"])
        except User.DoesNotExist:
            raise CommandError(f"пользователь «{options['by']}» не найден — подтверждать должен существующий человек")
        try:
            tenant = Tenant.objects.get(slug=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"салон «{options['tenant']}» не найден")
        if not SEED.exists():
            raise CommandError(f"нет seed-файла {SEED} — коды канона резолвить нечем")
        seed = load_seed()
        apply = options["apply"]
        include_disputed = options["include_disputed"]

        # Предмет — до первого числа.
        w = self.stdout.write
        w(f"предмет: {timezone.now().isoformat(timespec='seconds')} · база {connection.settings_dict['NAME']}"
          f"@{connection.settings_dict.get('HOST') or 'local'}")
        n_services = SalonService.objects.filter(tenant=tenant).count()
        w(f"салон: {tenant.slug} ({tenant.pk}) · услуг у салона: {n_services}")
        w(f"подтверждает: {who.username} ({who.pk}) · режим: {'APPLY' if apply else 'сухой прогон'}"
          f" · спорные строки: {'включены' if include_disputed else 'пропускаются'}")
        w(f"таблица: {len(ACTIONS)} строк из {TASK_DOC}, версия листа {LIST_VERSION}")
        w("")

        counts = {"applied": 0, "would_apply": 0, "done": 0, "disputed": 0, "blocked": 0}
        for action in ACTIONS:
            outcome = self._one(action, tenant, who, seed, apply=apply, include_disputed=include_disputed)
            counts[outcome] += 1

        w("")
        w(f"итог: {'записано' if apply else 'изменилось бы'} {counts['applied'] if apply else counts['would_apply']}"
          f" · уже сделано {counts['done']} · спорных пропущено {counts['disputed']}"
          f" · заблокировано {counts['blocked']}")
        if apply and counts["blocked"]:
            self.stderr.write(self.style.ERROR(
                f"{counts['blocked']} строк не исполнены по названным причинам — задание владельца закрыто не целиком"
            ))
            raise SystemExit(1)

    # ------------------------------------------------------------------

    def _one(self, a: Action, tenant, who, seed, *, apply: bool, include_disputed: bool) -> str:
        w = self.stdout.write
        head = f"#{a.n:>2} {a.service_name} → {a.code} (строка slice {a.slice_row})"

        service, why = salon_service_by_name(tenant, a.service_name)
        if service is None:
            w(self.style.ERROR(f"{head}\n     СТОП: {why}"))
            return "blocked"
        template, why = template_for_code(a.code, seed)
        if template is None:
            w(self.style.ERROR(f"{head}\n     услуга: {service.pk}\n     СТОП: {why}"))
            return "blocked"

        w(f"{head}\n     услуга: {service.pk} · сейчас mapping_status={service.mapping_status}, "
          f"template={service.template.name if service.template_id else '—'}")
        w(f"     канон: «{template.name}» ({template.category.name}) · "
          f"requires_health_check={template.requires_health_check}")
        if template.requires_health_check != a.expected_rhc:
            w(self.style.ERROR(
                f"     СТОП: RHC канона {template.requires_health_check} ≠ ожиданию задания {a.expected_rhc} — "
                "владельцу посмотреть на канон, команда не решает"
            ))
            return "blocked"

        already_verified = service.mapping_status == SalonService.MappingStatus.VERIFIED
        if already_verified and service.template_id == template.pk:
            w("     сделано ранее: VERIFIED с этим каноном — пропуск (идемпотентно)")
            return "done"
        if already_verified:
            w(self.style.ERROR(
                f"     СТОП: уже VERIFIED с другим каноном «{service.template.name}» — "
                "решено иначе, менять только руками"
            ))
            return "blocked"
        if service.mapping_status == SalonService.MappingStatus.NOT_RECOMMENDABLE:
            w(self.style.ERROR("     СТОП: NOT_RECOMMENDABLE — человек решил, что связи не будет; команда не отменяет"))
            return "blocked"

        if a.disputed and not include_disputed:
            w(self.style.WARNING(
                f"     пропуск: спорная строка — {a.disputed}; исполняется только с --include-disputed"
            ))
            return "disputed"

        source_ref = f"{LIST_VERSION} — строка {a.n} (slice {a.slice_row}); {TASK_DOC}"
        w(f"     будет: mapping_status=VERIFIED, template={a.code}, confirmed_by={who.username}, "
          f"source_ref=«{source_ref}»")
        if not apply:
            return "would_apply"

        self._write(service, template, who, source_ref)
        w(self.style.SUCCESS("     записано"))
        return "applied"

    @staticmethod
    def _write(service: SalonService, template: ServiceTemplate, who, source_ref: str) -> None:
        """Через форму §76 — те же отказы, что видит оператор в админке."""
        # ``|=``, а не ``.update()``: сторож в тестах считает любой ``.update(``
        # в этом модуле записью в базу мимо формы — и пусть считает.
        data = model_to_dict(service)
        data |= {
            "template": template.pk,
            "mapping_status": SalonService.MappingStatus.VERIFIED,
            "mapping_confirmed_by": who.pk,
            "mapping_confirmed_at": timezone.now(),
            "mapping_confirmed_rule": "",
            "mapping_rule_version": "",
            "mapping_source_ref": source_ref,
        }
        form = SalonServiceAdminForm(data=data, instance=service)
        if not form.is_valid():
            raise CommandError(
                f"форма §76 отказала для «{service.name}»: "
                + "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
            )
        with transaction.atomic():
            saved = form.save()
            SalonServiceAdmin._record_salon_wording_as_synonym(saved)
