"""Засев синтетического тестового набора для сквозной проверки Плана.

Решение владельца 08.10.2026: механику продуктового пути проверяют на
помеченных синтетических данных (``services.synthetic``). Здесь — единственное
место, где такие данные создаются: одной операцией и в порядке, которого
требуют замки базы (канон → способность → связь с целью → услуга салона →
тест-мастер → его предложение).

Содержание — в файле данных (``services/seeds/synthetic_plan_event.json``):
тот же файл показывается владельцу как пример плана. Здесь его только
проверяют и исполняют.

Правила исполнения:

* **Сначала проверки, потом запись.** Всё, что можно проверить до первой
  записи (цель, демо-салон, тестовая персона, категория, состав файла),
  проверяется до неё; отказ называет причину.
* **Повторный запуск ничего не дублирует и ничего не правит.** Найденная
  строка сверяется с файлом; расхождение — остановка с названной причиной.
  Пометка и основания синтетики неизменяемы, «починить» их молча нельзя.
* **Чужого не трогает.** Мастер заводится новый, только для теста; расписание
  существующих мастеров не читается и не меняется.
* **Одна транзакция.** Отказ на любом шаге не оставляет половины набора.

Модуль лежит рядом с командой, а не в ``services/``: это код засева — он
делает мастера продаваемым и заводит служебные учётки прямой записью, что
вне засева запрещено сторожами (правило продаваемости, имя учётки наружу).

Чего здесь нет: тестовой персоны и настроек стенда (их ставит оператор
стенда), записи, отправок.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from pathlib import Path

from django.db import IntegrityError, transaction
from django.utils import timezone

from services import capabilities
from services.models import (
    CapabilityGoalLink, GoalOption, ProcedureCapability, SalonService, ServiceCategory, ServiceTemplate,
    SpecialistService,
)
from services.synthetic import SYNTHETIC_RULE, grant_for
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.sellable import is_test_persona

DEFAULT_SPEC = Path(__file__).resolve().parents[2] / "seeds" / "synthetic_plan_event.json"

#: Служебный пользователь, которым подписан юридический класс синтетического
#: канона: у класса нет поля «правило», подтвердить его может только человек.
SERVICE_USERNAME = "synthetic-seed-service"
RULE_VERSION = "1"
WORKING_HOURS = (time(10, 0), time(20, 0))


class FixtureRefused(Exception):
    """Набор не засеян; ``reason`` — код причины, ``detail`` — что именно не так."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Report:
    """Что сделано и что из этого видно читателям."""

    created: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)
    real_capabilities_of_goal: int = 0
    master_id: str = ""
    master_place: str = ""
    visible_without_grant: list[str] = field(default_factory=list)
    visible_under_grant: list[str] | None = None
    grant_note: str = ""

    def note(self, created: bool, what: str) -> None:
        (self.created if created else self.found).append(what)


def load_spec(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or DEFAULT_SPEC).read_text(encoding="utf-8"))


# ─── проверки до записи ──────────────────────────────────────────────────────


def _preflight(spec: dict) -> dict:
    for key in ("goal_key", "salon_id", "test_persona_id", "templates", "capabilities", "offers", "test_master"):
        if not spec.get(key):
            raise FixtureRefused("spec_incomplete", f"в файле не заполнено «{key}»")
    for row in spec["templates"]:
        if not row.get("category_slug"):
            raise FixtureRefused("spec_incomplete", f"у канона «{row.get('name')}» не заполнено «category_slug»")

    goal = GoalOption.objects.filter(key=spec["goal_key"], is_active=True).first()
    if goal is None:
        raise FixtureRefused("goal_not_found", f"активной цели с ключом «{spec['goal_key']}» нет")
    categories = {}
    for row in spec["templates"]:
        category = ServiceCategory.objects.filter(slug=row["category_slug"]).first()
        if category is None:
            raise FixtureRefused("category_not_found", f"категории «{row['category_slug']}» нет")
        categories[row["ref"]] = category
    salon = Tenant.all_objects.filter(pk=spec["salon_id"]).first()
    if salon is None:
        raise FixtureRefused("salon_not_found", str(spec["salon_id"]))
    # Демо ли салон, здесь не читается: признак демо читают только через
    # владельца правила видимости (сторож DRF-2420), а судит о нём замок базы
    # «синтетика только в демо-салоне» — см. ``_offer``.
    persona = User.objects.filter(pk=spec["test_persona_id"]).first()
    if persona is None:
        raise FixtureRefused("test_persona_not_found", str(spec["test_persona_id"]))
    if not is_test_persona(persona):
        raise FixtureRefused("not_a_test_persona", f"у пользователя {persona.pk} нет признака тестовой персоны")

    refs = {row["ref"] for row in spec["templates"]}
    for capability in spec["capabilities"]:
        unknown = set(capability["templates"]) - refs
        if unknown:
            raise FixtureRefused("unknown_template_ref", f"{capability['key']}: {sorted(unknown)}")
    for offer in spec["offers"]:
        if offer["template"] not in refs:
            raise FixtureRefused("unknown_template_ref", f"услуга «{offer['name']}»: {offer['template']}")

    real = capabilities.capability_keys_helping_goal(goal.key)
    if real:
        raise FixtureRefused(
            "goal_has_real_knowledge",
            f"у цели «{goal.key}» уже есть подтверждённые настоящие способности ({len(real)}): в плане тестовой "
            "персоны настоящие и синтетические шаги смешались бы",
        )
    return {"goal": goal, "categories": categories, "salon": salon, "persona": persona}


# ─── шаги ────────────────────────────────────────────────────────────────────


def _same_or_refuse(what: str, row, expected: dict) -> None:
    for name, value in expected.items():
        stored = getattr(row, name)
        if stored != value:
            raise FixtureRefused(
                "differs_from_spec", f"{what}: поле {name} в базе {stored!r}, в файле {value!r}",
            )


def _service_user() -> tuple[User, bool]:
    user = User.objects.filter(username=SERVICE_USERNAME).first()
    if user is not None:
        return user, False
    user = User(username=SERVICE_USERNAME, is_active=False)
    user.set_unusable_password()
    user.save()
    return user, True


def _template(row: dict, category, service_user) -> tuple[ServiceTemplate, bool]:
    expected = {
        "synthetic": True,
        "category_id": category.pk,
        "body_care_scope": row["body_care_scope"],
        "legal_service_class": row["legal_service_class"],
        "required_practitioner_class": row.get("required_practitioner_class"),
        "requires_health_check": row["requires_health_check"],
        "scope_confirmed_rule": SYNTHETIC_RULE,
        "legal_class_source_ref": SYNTHETIC_RULE,
    }
    found = ServiceTemplate.objects.filter(name=row["name"]).first()
    if found is not None:
        _same_or_refuse(f"канон «{row['name']}»", found, expected)
        return found, False
    now = timezone.now()
    return ServiceTemplate.objects.create(
        name=row["name"], name_short=row["name"][:40], duration_default=row.get("duration_default"),
        scope_rule_version=RULE_VERSION, scope_confirmed_at=now, scope_source_ref=SYNTHETIC_RULE,
        legal_class_confirmed_by=service_user, legal_class_confirmed_at=now,
        health_check_origin=ServiceTemplate.HealthCheckOrigin.CONFIRMED,
        health_check_confirmed_rule=SYNTHETIC_RULE, health_check_rule_version=RULE_VERSION,
        health_check_confirmed_at=now, health_check_source_ref=SYNTHETIC_RULE,
        **{name: value for name, value in expected.items() if name != "category_id"}, category=category,
    ), True


def _claim_fields(spec: dict) -> dict:
    claim = spec.get("claim", {})
    return {
        "synthetic": True,
        "claim_scope": claim.get("claim_scope", "supported"),
        "claim_type": claim.get("claim_type", "product"),
        "evidence_kind": claim.get("evidence_kind", ""),
        "evidence_source": claim.get("evidence_source", SYNTHETIC_RULE),
        "source_ref": claim.get("source_ref", SYNTHETIC_RULE),
    }


def _capability(row: dict, templates: dict, claim: dict) -> tuple[ProcedureCapability, bool]:
    expected = {**claim, "text_client": row["text_client"], "expected_effect": row.get("expected_effect", "")}
    bound = sorted(templates[ref].pk for ref in row["templates"])
    found = ProcedureCapability.objects.filter(key=row["key"]).first()
    if found is not None:
        _same_or_refuse(f"способность «{row['key']}»", found, expected)
        stored = sorted(found.templates.values_list("pk", flat=True))
        if stored != bound:
            raise FixtureRefused("differs_from_spec", f"способность «{row['key']}»: привязана к другим канонам")
        return found, False
    return ProcedureCapability.objects.create(
        key=row["key"], templates=[templates[ref] for ref in row["templates"]], **expected,
    ), True


def _goal_link(capability, goal, claim: dict) -> tuple[CapabilityGoalLink, bool]:
    found = CapabilityGoalLink.objects.filter(capability=capability, goal=goal).first()
    if found is not None:
        _same_or_refuse(f"связь «{capability.key}» → «{goal.key}»", found, claim)
        return found, False
    return CapabilityGoalLink.objects.create(capability=capability, goal=goal, **claim), True


def _offer(row: dict, salon, category, template) -> tuple[SalonService, bool]:
    health = row["health_check"]
    expected = {
        "synthetic": True,
        "template_id": template.pk,
        "mapping_status": SalonService.MappingStatus.REVIEW_REQUIRED,
        "requires_health_check": health["answer"],
        "health_check_confirmed_rule": health.get("confirmed_rule", SYNTHETIC_RULE),
    }
    found = SalonService.objects.filter(tenant=salon, name=row["name"]).first()
    if found is not None:
        _same_or_refuse(f"услуга «{row['name']}»", found, expected)
        return found, False
    try:
        with transaction.atomic():
            return SalonService.objects.create(
                tenant=salon, category=category, name=row["name"], template=template,
                duration_minutes=row["duration_minutes"], base_price=Decimal(row["base_price"]),
                synthetic=True, mapping_status=expected["mapping_status"],
                requires_health_check=health["answer"],
                health_check_origin=SalonService.HealthCheckAnswerOrigin.CONFIRMED,
                health_check_confirmed_rule=expected["health_check_confirmed_rule"],
                health_check_rule_version=health.get("rule_version", RULE_VERSION),
                health_check_confirmed_at=timezone.now(), health_check_source_ref=SYNTHETIC_RULE,
            ), True
    except IntegrityError as error:
        if "synthetic_lives_only_in_a_demo_salon" in str(error):
            raise FixtureRefused("salon_is_not_demo", f"салон {salon.slug} не демонстрационный") from error
        raise


def _test_master(row: dict, salon) -> tuple[SpecialistProfile, bool]:
    """Мастер только для теста — новый, чтобы не трогать ничьё расписание.

    Заводится тем же механизмом, что и штатное заведение: пользователь с
    салоном, а профиль и связь «сотрудник» создаёт сигнал. Обе половины
    связи с салоном — с рождения.
    """
    from appointments.models import SpecialistWorkingHours

    account = row["account"]
    user = User.objects.filter(username=account).first()
    created = user is None
    if created:
        user = User(username=account, role="specialist", tenant=salon)
        user.set_unusable_password()
        user.save()
    profile = SpecialistProfile.objects.get(user=user)
    if not created:
        if profile.tenant_id != salon.pk:
            raise FixtureRefused("differs_from_spec", f"тест-мастер «{account}» числится в другом салоне")
        return profile, False
    profile.tenant = salon
    profile.display_name = row["display_name"]
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    # Место оказания услуг: только уже ПОДТВЕРЖДЁННОЕ место этого салона и
    # только если оно одно. Команда мест не создаёт и не подтверждает; без
    # места адрес в подтверждении записи будет пустым — это видно в отчёте.
    from tenants.service_location import LocationStatus, ServiceLocation

    confirmed = list(ServiceLocation.objects.filter(tenant=salon, status=LocationStatus.CONFIRMED)[:2])
    if len(confirmed) == 1:
        profile.works_at = confirmed[0]
    profile.save()
    if not TenantUserRelationship.objects.filter(
        user=user, tenant=salon, is_active=True, role=TenantUserRelationship.Role.STAFF,
    ).exists():
        raise FixtureRefused(
            "master_has_no_staff_relation", f"у тест-мастера «{account}» не появилась связь с салоном",
        )
    SpecialistWorkingHours.objects.bulk_create([
        SpecialistWorkingHours(
            specialist=profile, day_of_week=day, is_working_day=True,
            start_time=WORKING_HOURS[0], end_time=WORKING_HOURS[1],
        )
        for day in range(7)
    ])
    return profile, True


def _edge(master, offer, price: str) -> tuple[SpecialistService, bool]:
    found = SpecialistService.objects.filter(specialist=master, salon_service=offer).first()
    if found is not None:
        return found, False
    return SpecialistService.objects.create(
        specialist=master, salon_service=offer, duration_minutes=offer.duration_minutes, price=Decimal(price),
    ), True


# ─── исполнение ──────────────────────────────────────────────────────────────


class _DryRun(Exception):
    pass


def seed(spec: dict, *, dry_run: bool = False) -> Report:
    """Засеять набор. ``dry_run`` исполняет всё и откатывает транзакцию."""
    report = Report()
    try:
        with transaction.atomic():
            ctx = _preflight(spec)
            goal, categories, salon, persona = ctx["goal"], ctx["categories"], ctx["salon"], ctx["persona"]
            report.real_capabilities_of_goal = 0

            service_user, created = _service_user()
            report.note(created, f"служебный пользователь {SERVICE_USERNAME}")

            templates: dict = {}
            for row in spec["templates"]:
                templates[row["ref"]], created = _template(row, categories[row["ref"]], service_user)
                report.note(created, f"канон «{row['name']}»")

            claim = _claim_fields(spec)
            for row in spec["capabilities"]:
                capability, created = _capability(row, templates, claim)
                report.note(created, f"способность {row['key']}")
                _, created = _goal_link(capability, goal, claim)
                report.note(created, f"связь {row['key']} → {goal.key}")

            # Услуги раньше мастера: если салон не демо, отказ случится до
            # того, как в нём появится пользователь.
            offers = []
            for row in spec["offers"]:
                offer, created = _offer(row, salon, categories[row["template"]], templates[row["template"]])
                report.note(created, f"услуга «{row['name']}» в салоне {salon.slug}")
                offers.append((row, offer))

            master, created = _test_master(spec["test_master"], salon)
            report.master_id = str(master.pk)
            report.note(created, f"тест-мастер «{spec['test_master']['account']}» ({master.pk})")
            if created:
                report.created.append(
                    f"рабочие часы тест-мастера: ежедневно {WORKING_HOURS[0]:%H:%M}–{WORKING_HOURS[1]:%H:%M}"
                )
            report.master_place = (
                f"подтверждённое место салона ({master.works_at_id})" if master.works_at_id is not None
                else "НЕТ — у салона нет единственного подтверждённого места; адрес в подтверждении записи будет пустым"
            )

            for row, offer in offers:
                _, created = _edge(master, offer, row["master_price"])
                report.note(created, f"предложение тест-мастера по «{row['name']}»")

            report.visible_without_grant = list(capabilities.capability_keys_helping_goal(goal.key))
            grant = grant_for(persona)
            if grant is None:
                report.grant_note = (
                    "разрешение тестовой персоне сейчас не выдаётся: настройки стенда "
                    "(SYNTHETIC_TEST_DATA_ENABLED и SYNTHETIC_TEST_SUBJECT_IDS) не включают её"
                )
            else:
                report.visible_under_grant = list(
                    capabilities.capability_keys_helping_goal(goal.key, include_synthetic=grant)
                )
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return report
