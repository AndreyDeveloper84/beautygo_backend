"""Django Admin configuration for services app."""
from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.utils import timezone

from services.knowledge_intake import (
    APPROVED,
    course_errors,
    prohibition_errors,
    provenance_errors,
    timeframe_errors,
)
from services.mapping.store import StoredReport
from services.mapping.types import Decision
from services.mapping_review import confirm_single_candidate, evidence_text, mark_canon_gap
from services.normalization import normalize_service_name

from .models import (
    CanonGapRequest,
    CapabilityGoalLink,
    DraftSalonService,
    ExternalBusyInterval,
    ExternalSourceMapping,
    GoalDirection,
    GoalOption,
    GoalOptionCategory,
    ProcedureCapability,
    RegionalPricing,
    SalonService,
    Service,
    ServiceCategory,
    ServiceTemplate,
    ServiceTemplateSynonym,
    SpecialistService,
)


class ServiceInline(admin.TabularInline):
    model = Service
    extra = 0
    fields = ('name', 'price', 'duration_minutes', 'is_active')
    readonly_fields = ('created_at',)
    show_change_link = True


class ServiceTemplateInline(admin.TabularInline):
    model = ServiceTemplate
    extra = 0
    fields = (
        'name', 'name_short', 'duration_default',
        'duration_min', 'duration_max', 'is_popular', 'sort_order',
    )
    show_change_link = True


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'slug', 'icon', 'sort_order', 'is_active')
    list_filter = ('is_active', 'parent')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    ordering = ('sort_order', 'name')
    inlines = [ServiceTemplateInline]


class RegionalPricingInline(admin.TabularInline):
    model = RegionalPricing
    extra = 0
    fields = ('region_key', 'region_name', 'price_min', 'price_max')
    ordering = ('region_key',)


class ServiceTemplateSynonymInline(admin.TabularInline):
    model = ServiceTemplateSynonym
    extra = 0
    fields = (
        'text', 'source_tenant',
        'confirmed_by', 'confirmed_rule', 'rule_version',
        'confirmed_at', 'source_ref',
    )
    raw_id_fields = ('source_tenant', 'confirmed_by')
    ordering = ('text',)


@admin.register(ServiceTemplate)
class ServiceTemplateAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'canonical_code', 'lifecycle', 'category', 'duration_default',
        'is_popular', 'sort_order',
    )
    # `lifecycle` первым фильтром: очередь одобрения канонов — рабочий
    # список куратора справочника, ровно как очередь проверки связей у
    # оператора салонов (§93).
    list_filter = ('lifecycle', 'category', 'is_popular')
    # `synonyms__text` — то, ради чего синонимы и заведены (§93). Салон
    # называет услугу «Подмышки» в категории «Лазерная эпиляция», канон
    # называется «Лазерная эпиляция подмышек», и поиском по имени эта
    # пара не находится по устройству. Один записанный синоним делает
    # канон находимым словами салона — в том числе во всплывающем окне
    # выбора шаблона на форме услуги салона, потому что оно ищет этим же
    # набором полей.
    search_fields = ('name', 'name_short', 'category__name', 'synonyms__text', 'canonical_code')
    list_editable = ('is_popular', 'sort_order')
    ordering = ('category', '-is_popular', 'sort_order', 'name')
    inlines = [RegionalPricingInline, ServiceTemplateSynonymInline]

    def get_readonly_fields(self, request, obj=None):
        # MAP-AUTO-01: код, однажды поставленный, на форме не редактируется.
        # Пустой — можно заполнить (канон, заведённый оператором, получает
        # код только если владелец добавил его в эталонный список).
        base = tuple(super().get_readonly_fields(request, obj))
        if obj is not None and obj.canonical_code:
            return base + ('canonical_code',)
        return base


@admin.register(ServiceTemplateSynonym)
class ServiceTemplateSynonymAdmin(admin.ModelAdmin):
    list_display = (
        'text', 'template', 'source_tenant',
        'confirmed_by', 'confirmed_rule', 'confirmed_at',
    )
    list_filter = ('source_tenant', 'template__category')
    # `normalized` в поиске намеренно: оператор, ищущий «подмышки» и не
    # находящий «Подмышки», должен иметь возможность посмотреть, каким
    # ключом строка легла на самом деле.
    search_fields = ('text', 'normalized', 'template__name')
    raw_id_fields = ('template', 'source_tenant', 'confirmed_by')
    readonly_fields = ('normalized', 'created_at', 'updated_at')
    ordering = ('template', 'text')


@admin.register(RegionalPricing)
class RegionalPricingAdmin(admin.ModelAdmin):
    list_display = (
        'template', 'region_key', 'region_name',
        'price_min', 'price_max',
    )
    list_filter = ('region_key', 'template__category')
    search_fields = ('template__name', 'region_key', 'region_name')
    ordering = ('template__category', 'template', 'region_key')


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'get_specialist_name', 'category',
        'price', 'duration_minutes', 'is_active', 'created_at',
    )
    list_filter = ('category', 'is_active', 'created_at')
    search_fields = ('name', 'description', 'specialist__display_name', 'specialist__user__phone')
    readonly_fields = ('created_at',)
    raw_id_fields = ('specialist',)
    ordering = ('specialist', 'sort_order', 'name')

    @admin.display(description='Мастер')
    def get_specialist_name(self, obj: Service) -> str:
        return obj.specialist.display_name


# --- S3A canonical catalog rebuild (#1044 / #200) ---


class SpecialistServiceInline(admin.TabularInline):
    model = SpecialistService
    extra = 0
    # D2 nuance: requires_health_check editable so ops can set custom /
    # salon-specific health-check on off-taxonomy services.
    fields = (
        'specialist', 'duration_minutes', 'price',
        'requires_health_check', 'buffer_after_minutes', 'is_active',
    )
    raw_id_fields = ('specialist',)
    show_change_link = True


class ResolverDecisionFilter(admin.SimpleListFilter):
    """Фильтр «что ждёт владельца» — по отчётам dry-run всех салонов (файлы)."""

    title = 'исход резолвера'
    parameter_name = 'resolver'

    def lookups(self, request, model_admin):
        return [(d.value, d.value) for d in Decision] + [('NONE', 'нет прогона / нет в отчёте')]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        by_decision: dict[str, set[str]] = {}
        seen: set[str] = set()
        for report in StoredReport.load_all().values():
            for sid, row in report.rows.items():
                seen.add(sid)
                by_decision.setdefault(row['decision'], set()).add(sid)
        if value == 'NONE':
            return queryset.exclude(pk__in=seen)
        return queryset.filter(pk__in=by_decision.get(value, set()))


class SalonServiceAdminForm(forms.ModelForm):
    """Форма услуги салона: объясняет отказ вместо имени ограничения.

    Инвариант происхождения `VERIFIED` (§76) стоит `CheckConstraint`'ом в
    схеме, и Django с 4.1 проверяет его в `full_clean()` — то есть отказ
    форма даёт и без этого класса, строка до базы не доходит. Дубля тут
    нет: у двух сторожей разные адресаты.

    База отвечает системе и не обходится ни `update()`, ни миграцией
    данных. Форма отвечает **человеку**, и до этой правки отвечала так::

        {'__all__': ['Нарушено ограничение
                      "salonservice_verified_requires_provenance".']}

    Оператору, размечающему пятьдесят шесть услуг пилота (§93), отсюда
    видно, что он что-то нарушил, и не видно что. Ниже — то же самое
    правило, разложенное по полям: каждое сообщение висит на том поле,
    которое надо заполнить.

    Снимать нельзя ни одно из двух. Без базы инвариант обходится кодом,
    без формы — непониманием.
    """

    class Meta:
        model = SalonService
        fields = '__all__'

    #: Состояния, за которыми стоит РЕШЕНИЕ, а не его отсутствие. Оба
    #: несут автора, дату и основание, и по одной причине: решение без
    #: автора через месяц читается как умолчание.
    #:
    #: Список, а не сравнение с `VERIFIED`: когда §93 добавил четвёртое
    #: состояние, сравнение молча перестало покрывать половину случаев —
    #: отказ доезжал до базы и получал `IntegrityError` с именем
    #: ограничения, ровно то, что этот класс и чинил для подтверждения.
    #: Список хотя бы видно в диффе.
    DECIDED_STATUSES = frozenset({
        SalonService.MappingStatus.VERIFIED,
        SalonService.MappingStatus.NOT_RECOMMENDABLE,
    })

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('mapping_status') not in self.DECIDED_STATUSES:
            # Инвариант касается только решённых связей. Требовать
            # происхождение у каждой строки значило бы запретить заводить
            # обычную услугу: непроверенных на пилоте пятьдесят девять.
            return cleaned

        who = cleaned.get('mapping_confirmed_by')
        rule = (cleaned.get('mapping_confirmed_rule') or '').strip()
        version = (cleaned.get('mapping_rule_version') or '').strip()

        if cleaned.get('mapping_confirmed_at') is None:
            self.add_error('mapping_confirmed_at', forms.ValidationError(
                'Решённая связь обязана нести дату решения: без неё '
                '«проверено» и «не подлежит» не привязаны ко времени '
                'и не перепроверяются.',
                code='verified_requires_confirmed_at',
            ))

        if not (cleaned.get('mapping_source_ref') or '').strip():
            self.add_error('mapping_source_ref', forms.ValidationError(
                'Укажите основание: разбор, выгрузку, решение или тикет. '
                'Решение без основания через месяц неотличимо от '
                'умолчания.',
                code='verified_requires_source_ref',
            ))

        if who is None and not rule:
            # Сообщение вешается на «кто»: это выбор по умолчанию для
            # человека за экраном. Правило заполняет не он, а выгрузка.
            self.add_error('mapping_confirmed_by', forms.ValidationError(
                'Укажите, КТО принял решение, либо имя детерминированного '
                'правила в поле «mapping confirmed rule». Что-то одно '
                'обязательно.',
                code='verified_requires_who_or_rule',
            ))
        elif who is not None and rule:
            # База это ПРОПУСКАЕТ: `CheckConstraint` написан через `OR`,
            # а докстринг модели называет поля взаимоисключающими
            # («владелец назвал кто ИЛИ какое правило», §76).
            # Документированный инвариант без сторожа — здесь сторож
            # ставится в форме, потому что `services/models.py` занят
            # PR #307. Ужесточение самого ограничения — за срезом S1.
            self.add_error('mapping_confirmed_rule', forms.ValidationError(
                'Заполнено и «кто», и «правило». Происхождение должно быть '
                'одно: либо человек, либо правило — иначе неизвестно, что '
                'из двух решило.',
                code='verified_who_xor_rule',
            ))

        if rule and not version:
            self.add_error('mapping_rule_version', forms.ValidationError(
                'Правило без версии — «подтверждено какой-то из версий». '
                'Правила меняются, поэтому версия обязательна.',
                code='rule_requires_version',
            ))

        if (
            cleaned.get('mapping_status') == SalonService.MappingStatus.VERIFIED
            and cleaned.get('template') is None
        ):
            # База: `salonservice_verified_requires_template` (DRF-1668).
            # Здесь — по полю: «проверено» отвечает на вопрос «с чем
            # связана», и без шаблона ответа нет.
            self.add_error('template', forms.ValidationError(
                'Статус «проверено» подтверждает связь с канонической '
                'услугой — выберите шаблон. Если канона для этой услуги '
                'нет, это не «проверено», а «не подлежит рекомендациям» '
                'или разрыв канона (решение владельца).',
                code='verified_requires_template',
            ))

        return cleaned


@admin.register(SalonService)
class SalonServiceAdmin(admin.ModelAdmin):
    form = SalonServiceAdminForm
    # `mapping_status` стоит сразу за именем, а не в хвосте: разбор услуг
    # по §93 — это чтение статуса построчно, и он здесь главный столбец,
    # а не справочный. Рядом `mapping_confirmed_at`: статус без
    # происхождения читается как факт (§73), и оператор должен видеть,
    # что за «verified» что-то стоит.
    list_display = (
        'name', 'mapping_status', 'mapping_confirmed_at',
        'tenant', 'template', 'category',
        'duration_minutes', 'requires_health_check', 'is_active', 'source',
        # MAP-AUTO-05: исход резолвера из ОТЧЁТА последнего dry-run (файл),
        # не из базы — база статуса сверх SKIP_DECIDED не читает.
        'resolver_outcome', 'resolver_candidates', 'resolver_flags',
    )
    # Фильтр по статусу — рабочий инструмент очереди проверки: «покажи
    # всё, что ждёт подтверждения». Индекс `(tenant, mapping_status)` в
    # модели заведён под эту выборку и до сих пор был никем не спрошен.
    list_filter = (
        'mapping_status', ResolverDecisionFilter, 'is_active', 'source', 'requires_health_check', 'tenant',
    )
    search_fields = ('name', 'tenant__slug', 'template__name')
    raw_id_fields = ('template', 'category')
    readonly_fields = ('created_at', 'updated_at', 'resolver_evidence')
    ordering = ('tenant', 'name')
    inlines = [SpecialistServiceInline]
    actions = ('recompute_resolver_dry_run', 'confirm_single_resolver_candidate', 'mark_canon_gap')

    # ------------------------------------------------------------------
    # MAP-AUTO-05 — review-поверхность владельца поверх отчёта dry-run.
    # Отчёт — файл последнего прогона (`services/mapping/store.py`), один на
    # салон; колонки и блок на форме — только чтение; действия — решение
    # человека через ту же форму §76 (`services/mapping_review.py`),
    # `confirmed_by` = тот, кто нажал. Кнопки «применить всё» нет:
    # действие отказывает строке, где кандидатов не ровно один или стоят
    # флаги состава/здоровья, — и говорит почему.
    # ------------------------------------------------------------------

    def _report_row(self, obj):
        report = StoredReport.load(obj.tenant.slug)
        return report, (report.row(obj.pk) if report else None)

    @admin.display(description='Резолвер (dry-run)')
    def resolver_outcome(self, obj) -> str:
        report, row = self._report_row(obj)
        if report is None:
            return '— нет прогона'
        if row is None:
            return '— нет в отчёте'
        return f"{row['decision']} / {row['reason']}"

    @admin.display(description='Кандидаты')
    def resolver_candidates(self, obj) -> str:
        _, row = self._report_row(obj)
        if not row:
            return ''
        return '; '.join(
            f"{c.get('canonical_code') or '—'} {c['name']}" + (f" ←{c['rule']}" if c.get('rule') else '')
            for c in row.get('candidates', [])
        )

    @admin.display(description='Флаги')
    def resolver_flags(self, obj) -> str:
        _, row = self._report_row(obj)
        return ', '.join(row.get('flags', [])) if row else ''

    @admin.display(description='Резолвер: доказательства последнего dry-run')
    def resolver_evidence(self, obj) -> str:
        if obj.pk is None:
            return '—'
        report, row = self._report_row(obj)
        return evidence_text(row, report)

    def get_fieldsets(self, request, obj=None):
        fieldsets = list(super().get_fieldsets(request, obj))
        # Блок доказательств — отдельным разделом, чтобы оператор видел
        # его рядом с полями решения, а не терял в конце формы.
        fieldsets.append(('Резолвер (только чтение)', {'fields': ('resolver_evidence',)}))
        return fieldsets

    @admin.action(description='Пересчитать резолвером (dry-run, в файл отчёта; базу не трогает)')
    def recompute_resolver_dry_run(self, request, queryset):
        from services.mapping import RulesEnabled, resolve_tenant
        from services.mapping.schema import SchemaNotReady
        from services.mapping.store import store_report

        tenants = {s.tenant for s in queryset.select_related('tenant')}
        for tenant in sorted(tenants, key=lambda t: t.slug):
            try:
                rows = resolve_tenant(tenant, RulesEnabled(), report_ref=f'admin:{tenant.slug}')
            except SchemaNotReady as exc:
                self.message_user(request, f'{tenant.slug}: {exc}', level=messages.ERROR)
                continue
            path = store_report(rows, tenant.slug, rules='')
            self.message_user(
                request, f'{tenant.slug}: {len(rows)} строк → {path.name} (правила выключены: AUTO_NOT_ENABLED)',
            )

    def _run_review_action(self, request, queryset, fn, verb: str):
        written = refused = 0
        for service in queryset.select_related('tenant').order_by('name'):
            try:
                outcome = fn(service, request.user)
            except ValidationError as exc:
                refused += 1
                self.message_user(
                    request, f'«{service.name}»: форма §76 отказала — {exc.message}', level=messages.ERROR,
                )
                continue
            if outcome.written:
                written += 1
                self.message_user(request, f'«{outcome.name}»: {outcome.message}', level=messages.SUCCESS)
            else:
                refused += 1
                self.message_user(request, f'«{outcome.name}»: отказ — {outcome.message}', level=messages.WARNING)
        self.message_user(request, f'{verb}: записано {written}, отказано {refused}')

    @admin.action(description='Подтвердить связь с ЕДИНСТВЕННЫМ кандидатом резолвера (VERIFIED, я — подтверждающий)')
    def confirm_single_resolver_candidate(self, request, queryset):
        self._run_review_action(request, queryset, confirm_single_candidate, 'подтверждение')

    @admin.action(description='Отметить разрыв канона (NOT_RECOMMENDABLE с пометкой CANON_GAP, я — решающий)')
    def mark_canon_gap(self, request, queryset):
        self._run_review_action(request, queryset, mark_canon_gap, 'разрыв канона')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self._record_salon_wording_as_synonym(obj)

    @staticmethod
    def _record_salon_wording_as_synonym(service: SalonService) -> None:
        """Шаг 4 решения владельца §93, которого до сих пор не было.

        «Исходное название салона сохраняется как подтверждённый
        синоним» — это четвёртый шаг разбора, и до этой правки его не
        выполнял никто: таблица `ServiceTemplateSynonym` приехала со
        своей админкой и **без единого писателя**, ровно как `mapping_
        status` жил без писателя с §76 до §93.

        Почему это не выдумка за человека
        ---------------------------------

        Оператор, ставящий `VERIFIED`, утверждает буквально следующее:
        «услуга салона, которая называется вот так, — это вот этот
        канон». Синоним записывает то же самое утверждение и ничего
        сверх него. Провенанс копируется со связи целиком: тот же
        человек либо то же правило, та же дата, то же основание —
        потому что это и есть одно решение, а не два.

        Границу из §93 это не ломает: синоним по-прежнему **находит**
        канон и ничего не решает. Здесь он появляется как СЛЕД
        человеческого решения, а не как его причина.

        Три отказа, каждый со своей причиной
        ------------------------------------

        * **не `VERIFIED`** — записывать нечего: `REVIEW_REQUIRED` и
          `UNMAPPED` ничего не утверждают, а `NOT_RECOMMENDABLE` прямо
          говорит, что связи не будет, и синоним к канону из него не
          следует;
        * **нет шаблона** — внетаксономическая услуга (D2), канона у
          неё нет по устройству;
        * **синоним уже есть** — не переписываем. Чужой провенанс
          старше нашего, и затирать его значило бы объявить автором
          последнего сохранившего. Молча пропустить здесь правильно:
          строка уже говорит то, что мы хотели сказать.

        Почему в админке, а не в `save()` модели
        ----------------------------------------

        §93 описывает **операторский поток**, а не свойство строки.
        Писать синоним из `Model.save()` значило бы, что его создаёт и
        миграция, и сид, и любой скрипт — в том числе там, где
        «название салона» ничего не значит. Цена решения: связь,
        подтверждённая мимо админки, синонима не оставит. Это назван-
        ный размен, а не недосмотр; когда появится ручка подтверждения,
        она позовёт этот же метод.
        """
        if service.mapping_status != SalonService.MappingStatus.VERIFIED:
            return
        if service.template_id is None:
            return

        normalized = normalize_service_name(service.name)
        if not normalized:
            return

        already = ServiceTemplateSynonym.objects.filter(
            template_id=service.template_id, normalized=normalized,
        ).exists()
        if already:
            return

        ServiceTemplateSynonym.objects.create(
            template_id=service.template_id,
            text=service.name,
            source_tenant_id=service.tenant_id,
            confirmed_by=service.mapping_confirmed_by,
            confirmed_rule=service.mapping_confirmed_rule,
            rule_version=service.mapping_rule_version,
            confirmed_at=service.mapping_confirmed_at,
            source_ref=service.mapping_source_ref,
        )


@admin.register(SpecialistService)
class SpecialistServiceAdmin(admin.ModelAdmin):
    list_display = (
        'salon_service', 'specialist', 'tenant',
        'duration_minutes', 'price', 'requires_health_check', 'is_active',
    )
    list_filter = ('is_active', 'requires_health_check', 'tenant')
    search_fields = ('salon_service__name', 'specialist__display_name')
    raw_id_fields = ('salon_service', 'specialist')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'salon_service')


@admin.register(DraftSalonService)
class DraftSalonServiceAdmin(admin.ModelAdmin):
    list_display = (
        'external_name', 'tenant', 'status', 'external_source',
        'external_service_id', 'suggested_template', 'created_at',
    )
    list_filter = ('status', 'external_source', 'tenant')
    search_fields = ('external_name', 'external_service_id', 'tenant__slug')
    raw_id_fields = ('suggested_template', 'confirmed_salon_service', 'confirmed_by')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'status', 'external_name')


@admin.register(ExternalBusyInterval)
class ExternalBusyIntervalAdmin(admin.ModelAdmin):
    list_display = (
        'specialist', 'tenant', 'start_at', 'end_at',
        'source', 'external_id', 'received_at',
    )
    list_filter = ('source', 'tenant')
    search_fields = ('external_id', 'specialist__display_name', 'tenant__slug')
    raw_id_fields = ('tenant', 'specialist')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-start_at',)


@admin.register(ExternalSourceMapping)
class ExternalSourceMappingAdmin(admin.ModelAdmin):
    list_display = (
        'source', 'external_type', 'external_id', 'tenant',
        'salon_service', 'specialist',
    )
    list_filter = ('source', 'external_type', 'tenant')
    search_fields = ('external_id', 'tenant__slug')
    raw_id_fields = ('salon_service', 'specialist')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'external_type', 'external_id')


class GoalOptionCategoryInline(admin.TabularInline):
    model = GoalOptionCategory
    extra = 0
    fields = ('category', 'sort_order')
    ordering = ('sort_order',)


class GoalDirectionInline(admin.TabularInline):
    model = GoalDirection
    extra = 0
    fields = ('area_key', 'what', 'subline', 'sort_order', 'is_active')
    ordering = ('sort_order', 'area_key')


@admin.register(GoalOption)
class GoalOptionAdmin(admin.ModelAdmin):
    list_display = ('key', 'label', 'sort_order', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('key', 'label')
    list_editable = ('sort_order', 'is_active')
    ordering = ('sort_order', 'key')
    inlines = [GoalOptionCategoryInline, GoalDirectionInline]


@admin.register(GoalDirection)
class GoalDirectionAdmin(admin.ModelAdmin):
    list_display = ('goal_option', 'area_key', 'what', 'sort_order', 'is_active')
    list_filter = ('is_active', 'goal_option')
    search_fields = ('goal_option__key', 'goal_option__label', 'what')
    ordering = ('goal_option', 'sort_order', 'area_key')


@admin.register(GoalOptionCategory)
class GoalOptionCategoryAdmin(admin.ModelAdmin):
    list_display = ('goal_option', 'category', 'sort_order')
    list_filter = ('goal_option',)
    search_fields = ('goal_option__key', 'goal_option__label', 'category__name')
    raw_id_fields = ('category',)
    ordering = ('goal_option', 'sort_order')


# ── Заявки о разрыве канона (M9, DRF-1801, G6 / D6) ──────────────────────


class CanonGapRequestAdminForm(forms.ModelForm):
    """Решение владельца по заявке мастера — ошибки по полям, не имена ограничений."""

    class Meta:
        model = CanonGapRequest
        fields = ("status", "resolved_template", "clarification_question", "rejection_reason")

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("status")
        before = self.instance.status if self.instance and self.instance.pk else None
        if before in (CanonGapRequest.Status.APPROVED, CanonGapRequest.Status.REJECTED) and status != before:
            self.add_error("status", forms.ValidationError(
                "Решение уже принято и не перезаписывается: пересмотр — новая заявка мастера.",
                code="already_decided",
            ))
        if status == CanonGapRequest.Status.PENDING and before and before != CanonGapRequest.Status.PENDING:
            self.add_error("status", forms.ValidationError(
                "Вернуть заявку «на проверку» нельзя — выберите решение.", code="not_a_decision",
            ))
        if status == CanonGapRequest.Status.APPROVED and cleaned.get("resolved_template") is None:
            self.add_error("resolved_template", forms.ValidationError(
                "Подтверждение связывает заявку с каноническим шаблоном: найдите существующий "
                "или осознанно создайте новый и выберите его здесь.",
                code="template_required",
            ))
        if status == CanonGapRequest.Status.NEEDS_CLARIFICATION and not (
            cleaned.get("clarification_question") or ""
        ).strip():
            self.add_error("clarification_question", forms.ValidationError(
                "Напишите вопрос мастеру.", code="question_required",
            ))
        if status == CanonGapRequest.Status.REJECTED and not (cleaned.get("rejection_reason") or "").strip():
            self.add_error("rejection_reason", forms.ValidationError(
                "Напишите мастеру понятную причину.", code="reason_required",
            ))
        if status != CanonGapRequest.Status.APPROVED and cleaned.get("resolved_template") is not None:
            self.add_error("resolved_template", forms.ValidationError(
                "Шаблон называется только при подтверждении.", code="template_only_when_approved",
            ))
        return cleaned


@admin.register(CanonGapRequest)
class CanonGapRequestAdmin(admin.ModelAdmin):
    """Единственное место решения по заявкам (D6 — (а)): владелец в Django-admin.

    Заводит заявку мастер (бот), не сотрудник — добавления нет. Удаления нет:
    заявка и решение — след. Автор решения — всегда тот, кто сохранил
    (``request.user``), а не поле формы.
    """

    form = CanonGapRequestAdminForm
    list_display = ("name", "specialist", "tenant", "status", "resolved_template", "created_at", "decided_at")
    list_filter = ("status",)
    search_fields = ("name", "specialist__display_name")
    readonly_fields = (
        "specialist", "tenant", "name", "description", "duration_minutes", "price",
        "decided_by", "decided_at", "created_at", "updated_at",
    )
    fields = readonly_fields[:6] + (
        "status", "resolved_template", "clarification_question", "rejection_reason",
    ) + readonly_fields[6:]
    raw_id_fields = ("resolved_template",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        from services.canon_gap import CanonGapDecisionError, decide

        status = form.cleaned_data.get("status")
        if status == CanonGapRequest.Status.PENDING:
            return  # решения нет — писать нечего
        try:
            decide(
                obj,
                actor=request.user,
                status=status,
                template=form.cleaned_data.get("resolved_template"),
                question=form.cleaned_data.get("clarification_question") or "",
                reason=form.cleaned_data.get("rejection_reason") or "",
            )
        except CanonGapDecisionError as exc:
            from django.contrib import messages

            messages.error(request, str(exc))


# ── Знание о процедурах: возможность и её связь с целью (DRF-2606, DRF-2717) ──
#
# Путь ввода для куратора (владельца). Модели существовали с DRF-2606, но
# вписать в них строку было нечем: ни формы, ни команды. Здесь — форма.
#
# **Подтверждать вправе не каждый, кто вправе изменять** (решение владельца
# 02.10, блок A; DRF-2726). Право изменения даёт вести черновики. Сохранить
# строку подтверждённой, изменить подтверждённую, вернуть её в черновик или
# удалить может только тот, у кого есть отдельное право
# ``services.approve_<модель>``. Кому его дать, решает владелец — через группы
# и права в этой же админке; у суперпользователя оно есть всегда. Код имён не
# знает.
#
# Одно правило держит обе формы: **подтверждает тот, кто сохранил.** Полей
# «кто подтвердил» и «когда» в форме нет — их ставит ``save_model`` из
# ``request.user`` и текущего времени, как у заявок о разрыве канона выше.
# Подтверждение, вписанное рукой в поле, было бы утверждением о человеке, а не
# его действием. Источник (``source_ref``) форма требует сама, по полю и
# словами: база тоже откажет, но именем ограничения и уже после нажатия.

_CLAIM_READONLY = (
    "confirmed_by", "confirmed_at", "confirmed_rule", "rule_version",
    "created_at", "updated_at",
)

_CLAIM_FIELDSET = (
    "Утверждение и основание",
    {
        "fields": (
            "status", "claim_scope", "prohibited_statement", "limitations",
            "evidence_source", "evidence_kind", "source_ref", "valid_until",
        ),
        "description": (
            "«Подтверждено» можно сохранить только со ссылкой на источник. "
            "Автором подтверждения станет тот, кто сохраняет. "
            "Предмет запрета (prohibited statement) заполняется только у запрещённого "
            "утверждения: что именно нельзя утверждать. Он нужен внутреннему "
            "проверяющему; клиенту не говорится."
        ),
    },
)

_CONFIRMATION_FIELDSET = (
    "Подтверждение (ставится при сохранении)",
    {"fields": _CLAIM_READONLY},
)


def _may_approve(user, model) -> bool:
    """Есть ли у человека право подтверждать знание этой таблицы.

    Нет пользователя — нет права: форма, собранная вне админки, подтвердить
    не может.
    """
    if user is None:
        return False
    return user.has_perm(f"{model._meta.app_label}.approve_{model._meta.model_name}")


class _ClaimAdminForm(forms.ModelForm):
    """Общая проверка основания — ошибки по полям, а не имена ограничений базы."""

    #: Кто сохраняет. Ставит ``_ClaimAdmin.get_form`` на классе формы,
    #: собранном под один запрос.
    acting_user = None

    def clean(self):
        cleaned = super().clean()
        # Право подтверждения. ``self.instance`` здесь ещё несёт значения из
        # базы: форма переносит свои поля в экземпляр позже, в ``_post_clean``.
        was_approved = bool(self.instance.pk) and self.instance.status == APPROVED
        wants_approved = cleaned.get("status") == APPROVED
        untouched = was_approved and wants_approved and not self.changed_data
        if (was_approved or wants_approved) and not untouched:
            if not _may_approve(self.acting_user, self._meta.model):
                self.add_error(
                    "status",
                    forms.ValidationError(
                        "Подтверждать знание и менять подтверждённое может только тот, "
                        "кому владелец дал право подтверждения. Черновик можно сохранить "
                        "и без него."
                        if not was_approved
                        else "Эта строка подтверждена. Менять её и возвращать в черновик может "
                        "только тот, кому владелец дал право подтверждения.",
                        code="approval_right_required",
                    ),
                )
        # В форме подтверждающий есть всегда — это тот, кто сохраняет; его
        # впишет ``save_model``. Поэтому из двух условий базы форма спрашивает
        # одно: источник.
        for field, message in provenance_errors(
            status=cleaned.get("status") or "",
            source_ref=cleaned.get("source_ref") or "",
            has_confirmer=True,
        ).items():
            self.add_error(field, forms.ValidationError(message, code="provenance_required"))
        # ``text_client`` есть только у возможности; у связи с целью его нет.
        for field, message in prohibition_errors(
            claim_scope=cleaned.get("claim_scope") or "",
            prohibited_statement=cleaned.get("prohibited_statement") or "",
            text_client=cleaned.get("text_client") if "text_client" in self.fields else None,
        ).items():
            if not self.has_error(field):
                self.add_error(field, forms.ValidationError(message, code="prohibition_incomplete"))
        return cleaned


class ProcedureCapabilityAdminForm(_ClaimAdminForm):
    class Meta:
        model = ProcedureCapability
        fields = (
            "template", "key", "text_client", "text_professional",
            "expected_effect", "result_timeframe", "variability_note",
            "status", "claim_scope", "prohibited_statement", "limitations",
            "evidence_source", "evidence_kind", "source_ref", "valid_until",
        )

    def clean(self):
        cleaned = super().clean()
        for field, message in timeframe_errors(
            field="result_timeframe",
            value=cleaned.get("result_timeframe") or "",
            variability_note=cleaned.get("variability_note") or "",
            evidence_source=cleaned.get("evidence_source") or "",
            source_ref=cleaned.get("source_ref") or "",
        ).items():
            if not self.has_error(field):
                self.add_error(field, forms.ValidationError(message, code="timeframe_incomplete"))
        return cleaned


class CapabilityGoalLinkAdminForm(_ClaimAdminForm):
    class Meta:
        model = CapabilityGoalLink
        fields = (
            "capability", "goal", "course_pattern", "result_horizon", "variability_note",
            "status", "claim_scope", "prohibited_statement", "limitations",
            "evidence_source", "evidence_kind", "source_ref", "valid_until",
        )

    def clean(self):
        cleaned = super().clean()
        for field, message in course_errors(
            course_pattern=cleaned.get("course_pattern") or "",
            variability_note=cleaned.get("variability_note") or "",
            evidence_source=cleaned.get("evidence_source") or "",
            source_ref=cleaned.get("source_ref") or "",
        ).items():
            if not self.has_error(field):
                self.add_error(field, forms.ValidationError(message, code="course_incomplete"))
        for field, message in timeframe_errors(
            field="result_horizon",
            value=cleaned.get("result_horizon") or "",
            variability_note=cleaned.get("variability_note") or "",
            evidence_source=cleaned.get("evidence_source") or "",
            source_ref=cleaned.get("source_ref") or "",
        ).items():
            if not self.has_error(field):
                self.add_error(field, forms.ValidationError(message, code="timeframe_incomplete"))
        return cleaned


class _ClaimAdmin(admin.ModelAdmin):
    """Общее у двух таблиц знания: кто сохранил «подтверждено», тот и подтвердил."""

    readonly_fields = _CLAIM_READONLY
    list_filter = ("status", "claim_scope")

    def get_form(self, request, obj=None, **kwargs):
        # ``super().get_form`` собирает новый класс формы на каждый запрос —
        # пользователь записывается на нём, а не на общем классе.
        form = super().get_form(request, obj, **kwargs)
        form.acting_user = request.user
        return form

    def has_delete_permission(self, request, obj=None):
        # Удалить подтверждённое — то же, что отменить подтверждение.
        allowed = super().has_delete_permission(request, obj)
        if allowed and obj is not None and obj.status == obj.Status.APPROVED:
            return _may_approve(request.user, type(obj))
        return allowed

    def save_model(self, request, obj, form, change):
        if obj.status == obj.Status.APPROVED:
            # Сохранить подтверждённую строку с правкой — значит подтвердить
            # то, что в ней теперь написано: иначе новая формулировка осталась
            # бы под подтверждением прежней. Подтверждение правилом человек
            # при этом заменяет своим.
            #
            # Сохранение БЕЗ правок отметку не трогает: «Сохранить» на
            # открытой для чтения строке не должно переписывать, кто и когда
            # её подтвердил, — прежнего автора потом нигде не найти.
            already_confirmed = bool(obj.confirmed_by_id or obj.confirmed_rule)
            if not (change and already_confirmed and not form.changed_data):
                obj.confirmed_by = request.user
                obj.confirmed_at = timezone.now()
                obj.confirmed_rule = ""
                obj.rule_version = ""
        else:
            # Возврат в черновик снимает отметку: иначе у неподтверждённой
            # строки оставался бы автор подтверждения.
            obj.confirmed_by = None
            obj.confirmed_at = None
            obj.confirmed_rule = ""
            obj.rule_version = ""
        super().save_model(request, obj, form, change)


@admin.register(ProcedureCapability)
class ProcedureCapabilityAdmin(_ClaimAdmin):
    """Что процедура умеет — одна возможность одной процедуры (DRF-2606).

    ``key`` вводится руками и из формулировки не выводится (решение владельца
    29.09): автозаполнения из ``text_client`` здесь нет намеренно.
    """

    form = ProcedureCapabilityAdminForm
    list_display = ("template", "key", "status", "claim_scope", "valid_until", "confirmed_by")
    search_fields = ("key", "text_client", "template__name", "template__canonical_code")
    autocomplete_fields = ("template",)
    list_select_related = ("template", "confirmed_by")
    ordering = ("template", "key")
    fieldsets = (
        (
            "Возможность",
            {
                "fields": (
                    "template", "key", "text_client", "text_professional",
                    "expected_effect", "result_timeframe", "variability_note",
                ),
                "description": (
                    "Срок результата — словами, не числом, и только вместе с оговоркой "
                    "о разбросе, источником и ссылкой."
                ),
            },
        ),
        _CLAIM_FIELDSET,
        _CONFIRMATION_FIELDSET,
    )


@admin.register(CapabilityGoalLink)
class CapabilityGoalLinkAdmin(_ClaimAdmin):
    """Какой цели помогает возможность — отдельное утверждение со своим основанием."""

    form = CapabilityGoalLinkAdminForm
    list_display = ("capability", "goal", "status", "claim_scope", "valid_until", "confirmed_by")
    search_fields = ("capability__key", "capability__template__name", "goal__key", "goal__label")
    autocomplete_fields = ("capability", "goal")
    list_select_related = ("capability__template", "goal", "confirmed_by")
    ordering = ("capability", "goal")
    fieldsets = (
        (
            "Связь с целью",
            {
                "fields": (
                    "capability", "goal", "course_pattern", "result_horizon", "variability_note",
                ),
                "description": (
                    "Курс и горизонт результата — словами, не числом, и только вместе с "
                    "оговоркой о разбросе, источником и ссылкой."
                ),
            },
        ),
        _CLAIM_FIELDSET,
        _CONFIRMATION_FIELDSET,
    )
