"""Наполнить каталог демонстрационными салонами.

Зачем
-----
Замер пилота 30.08: один тенант, девять мастеров, и две цели из семи —
``event`` («собраться к событию») и ``new_look`` («обновить образ») —
находят **ноль** мастеров. Обе раскрываются в волосы, ногти, брови,
ресницы и макияж, а единственный боевой салон ведёт тело и массаж.
Клиент, выбравший такую подсказку, получает пустую полку: подсказка
показана, а выдачи за ней нет.

Команда заводит несколько салонов разного формата, чтобы у подбора
появилось из чего выбирать и чтобы многосалонность впервые проверялась
на данных, а не на одном тенанте.

Достоверность и вымысел — разные вопросы
----------------------------------------
Названия услуг, цены и длительности взяты из прайсов настоящих
российских салонов: провенанс лежит в ``meta.sources`` файла сида, а
ссылка на источник стоит у каждой строки в поле ``price_src``.

Названия салонов, имена мастеров и адреса — **вымышленные**. Платформа
принимает настоящие записи; салон, который о нас не знает, не должен в
ней оказаться, а мастер — обнаружить себя в чужом расписании. Телефоны
не заводятся вовсе: ``User.phone`` остаётся ``NULL``, потому что любой
правдоподобный номер — это чей-то настоящий номер.

Три замка, а не один
--------------------
Владелец просил завести салоны выключенными, чтобы подбор их не видел
до осознанного включения. Демо-данные заперты тремя независимыми
замками:

1. ``Tenant.is_active = False``                    — тенант вне обычных выборок;
2. ``SpecialistProfile.status = PENDING``          — движок берёт только ACTIVE;
3. ``SpecialistProfile.is_booking_enabled = False`` — и только с включённой записью.

Историческая справка, важная для понимания, почему замков три. Когда
сид писался, **одного** ``Tenant.is_active=False`` не хватало:
``RecommendationEngine`` фильтровал ``SpecialistProfile`` и к таблице
тенантов не присоединялся вовсе, так что мастер выключенного тенанта
попадал в выдачу как ни в чём не бывало. Менеджер ``Tenant.objects``
прячет строку лишь от того, кто спрашивает про тенанты, — а подбор про
тенанты не спрашивал. Держали только замки 2 и 3.

DRF-1430 это закрыл: движок соединяется с салоном, и замок 1 держит
подбор наравне с остальными
(``test_inactive_tenant_now_hides_masters_too``).

Замки 2 и 3 всё равно нужны, и не как перестраховка. Состояние салона
читает **только подбор**. Поиск (``search/views.py``) и публичный
каталог мастеров (``users/specialists_api.py``) к таблице тенантов не
присоединяются до сих пор — там демо-салон удерживает исключительно
замок 2 (``status``); замка 3 поиск не читает вовсе.

``--activate`` снимает все три разом и не создаёт ничего: включение —
отдельное решение владельца, и оно должно быть одним явным действием,
а не побочным эффектом сида.

Сухой прогон — поведение по умолчанию
-------------------------------------
Без ``--apply`` команда ничего не пишет. Печатает, сколько появится
салонов, мастеров, услуг и связей, и — главное — что станет с покрытием
целей: числа «до» берутся из живой базы, числа «после» считаются в
откатываемой транзакции тем же движком, который отвечает клиенту.
Иначе прогноз покрытия был бы мнением автора команды, а не замером.

Идемпотентность
---------------
Повторный запуск не плодит дубли: тенант ключуется по ``slug``,
пользователь по ``username``, салонная услуга по
``(tenant, template, name)``, связь «мастер ↔ услуга» по
``(specialist, salon_service)`` — всё это уникальные ключи в схеме.

Чужие тенанты
-------------
Команда трогает ТОЛЬКО слаги, объявленные в файле сида, и падает, если
среди них окажется слаг живого тенанта из ``PROTECTED_SLUGS`` (боевой
пилот ``formula-tela`` — там настоящие записи настоящих людей).

Использование::

    manage.py seed_demo_salons                  # сухой прогон
    manage.py seed_demo_salons --apply          # записать (выключенными)
    manage.py seed_demo_salons --activate       # сухой прогон включения
    manage.py seed_demo_salons --activate --apply
    manage.py seed_demo_salons --file <path>
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from services.goal_coverage import goal_master_coverage
from services.models import SalonService, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

DEFAULT_FILE = (
    Path(__file__).resolve().parents[2] / "seeds" / "demo_salons_2026-08.json"
)

# Боевой пилот. Слаг захардкожен как ЗАПРЕТ, а не как цель: список
# защищаемых расширяется флагом ``--protect``, но сузить его нельзя.
PROTECTED_SLUGS = frozenset({"formula-tela"})


class _Counts:
    """Счётчики созданного и переиспользованного — по полю на тип строки.

    Отдельный объект, а не кортеж: сухой прогон и запись считают одно и
    то же теми же полями, а «положительная стража» в тестах читает их по
    имени, а не по позиции.
    """

    __slots__ = (
        "tenants",
        "users",
        "specialists",
        "salon_services",
        "specialist_services",
        "reused_tenants",
        "reused_users",
        "reused_salon_services",
        "reused_specialist_services",
    )

    def __init__(self) -> None:
        for name in self.__slots__:
            setattr(self, name, 0)

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


class _Rollback(Exception):
    """Носитель отката для сухого прогона — наружу не выходит."""


class Command(BaseCommand):
    help = (
        "Seed demo salons (fictional names, sourced prices) with every "
        "safety lock ON. Dry-run by default; --apply writes."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--file", default=str(DEFAULT_FILE))
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without it the command only reports.",
        )
        parser.add_argument(
            "--activate",
            action="store_true",
            help=(
                "Owner-only: lift all three safety locks on the demo "
                "tenants. Creates nothing. Still needs --apply to write."
            ),
        )
        parser.add_argument(
            "--protect",
            action="append",
            default=[],
            help=(
                "Extra tenant slug the seed must never touch. "
                "'formula-tela' is always protected."
            ),
        )

    # ------------------------------------------------------------------
    def handle(self, *args, **options) -> None:
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"Seed file not found: {path}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {path}: {exc}") from exc

        salons = document["salons"]
        protected = PROTECTED_SLUGS | set(options["protect"])
        self._refuse_protected(salons, protected)
        self._refuse_unresolved_templates(salons)

        apply_changes = options["apply"]
        before = goal_master_coverage()

        if options["activate"]:
            self._run_activate(salons, apply_changes, before)
        else:
            self._run_seed(salons, apply_changes, before)

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    @staticmethod
    def _refuse_protected(salons: list[dict], protected: set[str]) -> None:
        """Файл не вправе назвать слаг живого тенанта.

        Пилотный ``formula-tela`` несёт настоящие записи настоящих
        людей. Совпадение слага означало бы, что сид дописывает мастеров
        и услуги в боевой салон — молча и необратимо.
        """
        clash = sorted({s["slug"] for s in salons} & protected)
        if clash:
            raise CommandError(
                "Seed file names protected tenant slug(s): "
                + ", ".join(clash)
                + ". Demo data must never be written into a live tenant."
            )

    @staticmethod
    def _refuse_unresolved_templates(salons: list[dict]) -> None:
        """Каждая услуга обязана лечь на канонический шаблон.

        Ключ — пара ``(категория, название)``: 50 названий шаблонов
        встречаются больше чем в одной категории («Мужская стрижка» —
        и в «Стрижках», и в «Мужском уходе»), поэтому по одному имени
        шаблон однозначно не определяется.

        Падаем со списком всех непопавших сразу: молчаливый пропуск
        оставил бы услугу без категории, то есть вне цели — ровно та
        дыра, которую команда закрывает.
        """
        wanted = {
            (svc["category"], svc["template"])
            for salon in salons
            for svc in salon["services"]
        }
        known = set(
            ServiceTemplate.objects.filter(
                category__name__in={c for c, _ in wanted},
                name__in={n for _, n in wanted},
            ).values_list("category__name", "name")
        )
        missing = sorted(wanted - known)
        if missing:
            raise CommandError(
                "Unresolved (category, template) pairs — seed the canonical "
                "catalog first (manage.py seed_canonical_catalog):\n"
                + "\n".join(f"  {cat} -> {name}" for cat, name in missing)
            )

    # ------------------------------------------------------------------
    # seed
    # ------------------------------------------------------------------
    def _run_seed(
        self,
        salons: list[dict],
        apply_changes: bool,
        before: dict[str, int],
    ) -> None:
        if apply_changes:
            with transaction.atomic():
                counts = self._seed(salons)
            after = self._coverage_if_activated(salons)
            self._report(counts, before, after, applied=True)
            return

        counts, after = self._preview(salons)
        self._report(counts, before, after, applied=False)

    def _preview(self, salons: list[dict]) -> tuple[_Counts, dict[str, int]]:
        """Записать, померить, откатить.

        Прогноз покрытия обязан быть измерением, а не оценкой: движок
        отвечает на реальных строках, поэтому сухой прогон их создаёт и
        выбрасывает.
        """
        box: dict = {}
        try:
            with transaction.atomic():
                box["counts"] = self._seed(salons)
                # Замки сняты только внутри отката: покрытие «после»
                # отвечает на вопрос «что цель найдёт, когда владелец
                # включит», а не «что она находит, пока выключено».
                self._activate(salons)
                box["after"] = goal_master_coverage()
                raise _Rollback
        except _Rollback:
            pass
        return box["counts"], box["after"]

    def _coverage_if_activated(self, salons: list[dict]) -> dict[str, int]:
        """Покрытие, которое подбор увидит ПОСЛЕ ``--activate``.

        После записи с замками покрытие ещё нулевое — в этом и смысл
        замков. Печатать его как результат значило бы отчитаться, что
        команда ничего не изменила.
        """
        box: dict = {}
        try:
            with transaction.atomic():
                self._activate(salons)
                box["after"] = goal_master_coverage()
                raise _Rollback
        except _Rollback:
            pass
        return box["after"]

    def _seed(self, salons: list[dict]) -> _Counts:
        counts = _Counts()
        for salon in salons:
            tenant = self._upsert_tenant(salon, counts)
            specialists = self._upsert_specialists(salon, tenant, counts)
            self._upsert_services(salon, tenant, specialists, counts)
        return counts

    @staticmethod
    def _upsert_tenant(salon: dict, counts: _Counts) -> Tenant:
        # all_objects: обычный менеджер прячет is_active=False, то есть
        # второй прогон не нашёл бы созданный первым тенант и упал бы на
        # уникальном слаге. Идемпотентность здесь держится именно на
        # выборе менеджера.
        tenant, created = Tenant.all_objects.get_or_create(
            slug=salon["slug"],
            defaults={"name": salon["name"], "is_active": False},
        )
        if created:
            counts.tenants += 1
        else:
            counts.reused_tenants += 1
            # Имя обновляем, is_active — никогда: владелец мог включить
            # салон осознанно, и повторный сид не вправе это отменить.
            if tenant.name != salon["name"]:
                tenant.name = salon["name"]
                tenant.save(update_fields=["name"])
        return tenant

    def _upsert_specialists(
        self,
        salon: dict,
        tenant: Tenant,
        counts: _Counts,
    ) -> dict[str, SpecialistProfile]:
        profiles: dict[str, SpecialistProfile] = {}
        for person in salon["specialists"]:
            user, created = User.objects.get_or_create(
                username=person["username"],
                defaults={
                    "role": "specialist",
                    # phone остаётся NULL сознательно: правдоподобный
                    # номер — это чей-то настоящий номер.
                    "phone": None,
                    "is_verified": False,
                    "tenant": tenant,
                },
            )
            if created:
                counts.users += 1
                counts.specialists += 1
            else:
                counts.reused_users += 1
                if user.tenant_id != tenant.id:
                    user.tenant = tenant
                    user.save(update_fields=["tenant"])

            # Профиль создаёт post_save-сигнал ``users.signals`` при
            # role='specialist'; свой create() дал бы IntegrityError на
            # OneToOne. Поэтому только дополняем созданный сигналом.
            profile = SpecialistProfile.objects.get(user=user)
            profile.tenant = tenant
            profile.display_name = person["display_name"]
            # Специальности мастера нет отдельного поля, а состав салона
            # — половина ответа на вопрос «есть ли из чего выбирать».
            # Поэтому роль едет в начало био, а не лежит в файле мёртвым
            # комментарием.
            role = person.get("role", "")
            about = person.get("bio", "")
            profile.bio = f"{role} — {about}" if role and about else (role or about)
            profile.experience_years = person.get("experience_years", 0)
            profile.address = salon["address"]
            profile.timezone = salon.get("timezone", "Europe/Moscow")
            profile.rating = Decimal(person["rating"])
            # reviews_count остаётся 0: рейтинг нужен движку (порог
            # AI_SPECIALIST_MIN_RATING = 4.0), а выдуманные отзывы — это
            # выдуманное социальное доказательство. Их не будет.
            profile.reviews_count = 0
            if profile.status != SpecialistProfile.ProfileStatus.ACTIVE:
                # Уже включённого владельцем мастера сид не выключает.
                profile.status = SpecialistProfile.ProfileStatus.PENDING
                profile.is_booking_enabled = False
            profile.is_available = True
            profile.save()
            profiles[person["ref"]] = profile
        return profiles

    def _upsert_services(
        self,
        salon: dict,
        tenant: Tenant,
        specialists: dict[str, SpecialistProfile],
        counts: _Counts,
    ) -> None:
        templates = self._template_map(salon)
        for svc in salon["services"]:
            template = templates[(svc["category"], svc["template"])]
            name = svc.get("name", svc["template"])
            salon_service, created = SalonService.objects.get_or_create(
                tenant=tenant,
                template=template,
                name=name,
                defaults={
                    # Категория шаблона, а не корень: цели курируются на
                    # корнях и раскрываются вниз до листьев, поэтому
                    # услуга обязана висеть на листе, иначе цель её не
                    # найдёт (DRF-1308).
                    "category": template.category,
                    "duration_minutes": svc["duration_minutes"],
                    "base_price": Decimal(str(svc["price"])),
                    "requires_health_check": template.requires_health_check,
                    "is_active": True,
                    "source": SalonService.Source.SEED,
                },
            )
            counts.salon_services += int(created)
            counts.reused_salon_services += int(not created)

            for ref in svc["by"]:
                try:
                    specialist = specialists[ref]
                except KeyError:
                    raise CommandError(
                        f"{salon['slug']}: service {name!r} assigned to "
                        f"unknown specialist ref {ref!r}."
                    )
                _, made = SpecialistService.objects.get_or_create(
                    salon_service=salon_service,
                    specialist=specialist,
                    defaults={
                        "tenant": tenant,
                        "price": Decimal(str(svc["price"])),
                        "duration_minutes": svc["duration_minutes"],
                        "requires_health_check": (
                            salon_service.requires_health_check
                        ),
                        "is_active": True,
                    },
                )
                counts.specialist_services += int(made)
                counts.reused_specialist_services += int(not made)

    @staticmethod
    def _template_map(salon: dict) -> dict[tuple[str, str], ServiceTemplate]:
        pairs = {(s["category"], s["template"]) for s in salon["services"]}
        return {
            (t.category.name, t.name): t
            for t in ServiceTemplate.objects.select_related("category").filter(
                category__name__in={c for c, _ in pairs},
                name__in={n for _, n in pairs},
            )
            if (t.category.name, t.name) in pairs
        }

    # ------------------------------------------------------------------
    # activate
    # ------------------------------------------------------------------
    def _run_activate(
        self,
        salons: list[dict],
        apply_changes: bool,
        before: dict[str, int],
    ) -> None:
        slugs = [s["slug"] for s in salons]
        known = set(
            Tenant.all_objects.filter(slug__in=slugs).values_list(
                "slug", flat=True
            )
        )
        absent = sorted(set(slugs) - known)
        if absent:
            raise CommandError(
                "Nothing to activate — these demo tenants do not exist yet "
                "(run the seed first): " + ", ".join(absent)
            )

        if apply_changes:
            with transaction.atomic():
                n_tenants, n_specialists = self._activate(salons)
            after = goal_master_coverage()
        else:
            box: dict = {}
            try:
                with transaction.atomic():
                    box["counts"] = self._activate(salons)
                    box["after"] = goal_master_coverage()
                    raise _Rollback
            except _Rollback:
                pass
            n_tenants, n_specialists = box["counts"]
            after = box["after"]

        prefix = "" if apply_changes else "[dry-run] "
        self.stdout.write(
            self.style.WARNING(
                f"{prefix}activate: {n_tenants} tenant(s), "
                f"{n_specialists} specialist(s) — all three locks lifted"
            )
        )
        self._print_coverage(before, after)
        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    "[dry-run] nothing written — re-run with --apply"
                )
            )

    @staticmethod
    def _activate(salons: list[dict]) -> tuple[int, int]:
        slugs = [s["slug"] for s in salons]
        tenant_ids = list(
            Tenant.all_objects.filter(slug__in=slugs).values_list(
                "id", flat=True
            )
        )
        n_tenants = Tenant.all_objects.filter(slug__in=slugs).update(
            is_active=True
        )
        n_specialists = SpecialistProfile.objects.filter(
            tenant_id__in=tenant_ids
        ).update(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
            is_booking_enabled=True,
            is_available=True,
        )
        return n_tenants, n_specialists

    # ------------------------------------------------------------------
    # report
    # ------------------------------------------------------------------
    def _report(
        self,
        counts: _Counts,
        before: dict[str, int],
        after: dict[str, int],
        *,
        applied: bool,
    ) -> None:
        prefix = "" if applied else "[dry-run] "
        style = self.style.SUCCESS if applied else self.style.WARNING
        self.stdout.write(
            style(
                f"{prefix}salons={counts.tenants} "
                f"masters={counts.specialists} "
                f"salon_services={counts.salon_services} "
                f"bookable_links={counts.specialist_services}"
            )
        )
        reused = (
            counts.reused_tenants
            + counts.reused_users
            + counts.reused_salon_services
            + counts.reused_specialist_services
        )
        if reused:
            self.stdout.write(
                f"{prefix}reused (idempotent): "
                f"tenants={counts.reused_tenants} "
                f"users={counts.reused_users} "
                f"salon_services={counts.reused_salon_services} "
                f"links={counts.reused_specialist_services}"
            )
        self._print_coverage(before, after)
        if applied:
            self.stdout.write(
                self.style.SUCCESS(
                    "written with ALL THREE safety locks ON — tenant "
                    "inactive, masters pending, booking disabled. The "
                    "coverage above is what the picker WILL see once "
                    "--activate is run; right now it still sees the "
                    "'before' column."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}nothing written — re-run with --apply"
                )
            )

    def _print_coverage(
        self,
        before: dict[str, int],
        after: dict[str, int],
    ) -> None:
        self.stdout.write(
            "goal coverage (masters the picker returns), now -> once active:"
        )
        for key in after:
            was, now = before.get(key, 0), after[key]
            if was == 0 and now > 0:
                mark = "  <- hole closed"
            elif now > was:
                mark = "  <- deepened"
            else:
                mark = ""
            self.stdout.write(f"  {key:<12} {was:>3} -> {now:>3}{mark}")
