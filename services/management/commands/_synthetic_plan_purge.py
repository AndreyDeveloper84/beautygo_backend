"""Очистка синтетического тестового набора Плана — обратный ход засева.

Решение владельца 10.10.2026: команда безопасной очистки с пробным прогоном.
Сама она не запускается ничем: ни слиянием, ни выкладкой, ни засевом.

Что считается набором — то же, что засевает ``_synthetic_plan_fixture``, и
находится по тем же именам из того же файла данных: каноны по названию,
способности по ключу, услуга по салону и названию, тест-мастер и служебный
пользователь по имени учётки. Идентификаторы у строк случайные, имён других
нет.

Правила:

* **Только помеченное.** Канон, способность или услуга с именем из набора,
  но без пометки синтетики, — остановка: это не строка набора, и трогать её
  нельзя. Учётка с именем тест-мастера, которая не мастер ЭТОГО салона, —
  то же.
* **Созданное прогоном не удаляется.** Запись к тест-мастеру, выбор услуги в
  шаге плана, сами планы тестовой персоны — следы проверки, у них свои
  правила хранения и свои владельцы. Команда их считает и называет.
* **Что держат следы прогона — остаётся и выключается.** База не даёт
  удалить услугу и мастера, на которых ссылается запись или выбор шага. Такая
  строка остаётся; тест-мастер при этом выводится из продажи (не принимает
  записи, не действующий, вход закрыт) — записаться к нему больше нельзя.
* **Оставленный тест-мастер остаётся скрытым.** Из обычной выдачи боту и
  клиентам его исключает признак «у мастера есть предложение по помеченной
  услуге» (``users.sellable.synthetic_offer_master_q``, решение владельца
  10.10.2026). Поэтому у мастера, которого удалить нельзя, это предложение
  НЕ удаляется: без него он вернулся бы в выдачу обычным мастером
  демо-салона.
* **Одна транзакция.** Пробный прогон исполняет всё и откатывает её.

Чего команда не делает и не может: строка тест-мастера в зеркале каталога у
бота, журналы и уже отправленные события — вне каталога; настройки стенда
(разрешение тестовой персоне) — у оператора стенда.

Модуль лежит рядом с командой по той же причине, что и засев: он меняет
продаваемость мастера и удаляет учётки прямой записью.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, RestrictedError

from services.models import ProcedureCapability, SalonService, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

from ._synthetic_plan_fixture import SERVICE_USERNAME, FixtureRefused


@dataclass
class PurgeReport:
    """Что удалено, что осталось и почему."""

    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    #: Следы прогона, которые команда не трогает: «что» → сколько.
    run_traces: dict[str, int] = field(default_factory=dict)
    #: Что ушло каскадом вместе с названными строками: модель → сколько.
    cascaded: Counter = field(default_factory=Counter)


# ─── удаление, которое не настаивает ────────────────────────────────────────


def _try_delete(row) -> str | None:
    """Удалить строку, если база позволяет. Возвращает ``None`` или то, что её держит."""
    try:
        with transaction.atomic():
            _, per_model = row.delete()
    except (ProtectedError, RestrictedError) as held:
        holders = Counter(type(obj)._meta.verbose_name for obj in getattr(held, "protected_objects", ()) or ())
        holders.update(type(obj)._meta.verbose_name for obj in getattr(held, "restricted_objects", ()) or ())
        return ", ".join(f"{name} — {count}" for name, count in sorted(holders.items())) or "ссылки из других таблиц"
    except IntegrityError as held:
        return f"замок базы: {str(held).splitlines()[0][:160]}"
    return per_model


def _remove(report: PurgeReport, what: str, row) -> bool:
    outcome = _try_delete(row)
    if isinstance(outcome, str):
        report.kept.append(f"{what} — держат: {outcome}")
        return False
    own = row._meta.label
    for label, count in outcome.items():
        if label != own:
            report.cascaded[label] += count
    report.removed.append(what)
    return True


def _synthetic_or_refuse(what: str, row) -> None:
    if not row.synthetic:
        raise FixtureRefused("not_a_synthetic_row", f"{what} есть в базе, но не помечена как синтетика — не трогаю")


# ─── следы прогона ──────────────────────────────────────────────────────────


def _run_traces(offers: list[SalonService], master: SpecialistProfile | None) -> dict[str, int]:
    from appointments.models import Appointment
    from wellness.models import PlanStepResolution

    traces: dict[str, int] = {}
    offer_ids = [offer.pk for offer in offers]
    if offer_ids:
        traces["записи на синтетическую услугу"] = Appointment.objects.filter(salon_service_id__in=offer_ids).count()
        traces["выбор синтетической услуги в шаге плана"] = PlanStepResolution.objects.filter(
            tenant_offer_id__in=offer_ids,
        ).count()
    if master is not None:
        traces["записи к тест-мастеру"] = Appointment.objects.filter(specialist=master).count()
    return {what: count for what, count in traces.items() if count}


def _has_bookings(master: SpecialistProfile) -> bool:
    from appointments.models import Appointment

    return Appointment.objects.filter(specialist=master).exists()


# ─── тест-мастер ────────────────────────────────────────────────────────────


def _master_of(spec: dict, salon: Tenant | None) -> tuple[User | None, SpecialistProfile | None]:
    account = spec["test_master"]["account"]
    user = User.objects.filter(username=account).first()
    if user is None:
        return None, None
    profile = SpecialistProfile.objects.filter(user=user).first()
    if profile is None or salon is None or profile.tenant_id != salon.pk:
        raise FixtureRefused(
            "not_the_seeded_master", f"учётка «{account}» есть, но это не мастер салона набора — не трогаю",
        )
    return user, profile


def _take_off_sale(report: PurgeReport, account: str, user: User, profile: SpecialistProfile) -> None:
    """Мастер, которого удалить нельзя: больше не продаётся и не входит."""
    from appointments.models import SpecialistWorkingHours

    profile.is_booking_enabled = False
    profile.is_available = False
    profile.status = SpecialistProfile.ProfileStatus.DRAFT
    profile.save(update_fields=["is_booking_enabled", "is_available", "status"])
    hours, _ = SpecialistWorkingHours.objects.filter(specialist=profile).delete()
    if user.is_active:
        user.is_active = False
        user.save(update_fields=["is_active"])
    report.disabled.append(
        f"тест-мастер «{account}» ({profile.pk}): не принимает записи, не действующий, вход закрыт; "
        f"рабочих часов удалено — {hours}"
    )


# ─── исполнение ─────────────────────────────────────────────────────────────


class _DryRun(Exception):
    pass


def purge(spec: dict, *, dry_run: bool) -> PurgeReport:
    """Убрать набор. ``dry_run`` исполняет всё и откатывает транзакцию."""
    report = PurgeReport()
    for key in ("salon_id", "templates", "capabilities", "offers", "test_master"):
        if not spec.get(key):
            raise FixtureRefused("spec_incomplete", f"в файле не заполнено «{key}»")
    try:
        with transaction.atomic():
            salon = Tenant.all_objects.filter(pk=spec["salon_id"]).first()

            # Сначала найти всё и убедиться, что это строки набора: отказ
            # здесь не оставляет половины работы.
            offers = []
            for row in spec["offers"]:
                offer = SalonService.objects.filter(tenant=salon, name=row["name"]).first() if salon else None
                if offer is None:
                    report.absent.append(f"услуга «{row['name']}»")
                    continue
                _synthetic_or_refuse(f"услуга «{row['name']}»", offer)
                offers.append(offer)
            capabilities = []
            for row in spec["capabilities"]:
                capability = ProcedureCapability.objects.filter(key=row["key"]).first()
                if capability is None:
                    report.absent.append(f"способность {row['key']}")
                    continue
                _synthetic_or_refuse(f"способность {row['key']}", capability)
                capabilities.append(capability)
            templates = []
            for row in spec["templates"]:
                template = ServiceTemplate.objects.filter(name=row["name"]).first()
                if template is None:
                    report.absent.append(f"канон «{row['name']}»")
                    continue
                _synthetic_or_refuse(f"канон «{row['name']}»", template)
                templates.append(template)
            account = spec["test_master"]["account"]
            user, master = _master_of(spec, salon)
            if user is None:
                report.absent.append(f"тест-мастер «{account}»")

            report.run_traces = _run_traces(offers, master)
            # Мастера, к которому есть запись, база удалить не даст: он
            # остаётся — и вместе с ним его предложение, по которому он скрыт.
            master_stays = master is not None and _has_bookings(master)

            # Порядок — обратный засеву: предложение мастера → услуга →
            # способности (привязки и связи с целью уходят каскадом) → каноны
            # → тест-мастер → служебный пользователь.
            for offer in offers:
                hides_the_master = False
                for edge in SpecialistService.objects.filter(salon_service=offer):
                    if master_stays and edge.specialist_id == master.pk:
                        hides_the_master = True
                        report.kept.append(
                            f"предложение мастера по «{offer.name}» — оставлено намеренно: тест-мастер остаётся "
                            "в базе, а из обычной выдачи его исключает именно это предложение"
                        )
                        continue
                    _remove(report, f"предложение мастера по «{offer.name}»", edge)
                if hides_the_master:
                    report.kept.append(f"услуга «{offer.name}» — держит оставленное предложение тест-мастера")
                else:
                    _remove(report, f"услуга «{offer.name}»", offer)
            for capability in capabilities:
                _remove(report, f"способность {capability.key}", capability)
            for template in templates:
                _remove(report, f"канон «{template.name}»", template)
            if user is not None:
                if not _remove(report, f"тест-мастер «{account}» ({master.pk})", user):
                    _take_off_sale(report, account, user, master)

            service_user = User.objects.filter(username=SERVICE_USERNAME).first()
            if service_user is None:
                report.absent.append(f"служебный пользователь {SERVICE_USERNAME}")
            else:
                _remove(report, f"служебный пользователь {SERVICE_USERNAME}", service_user)

            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return report
