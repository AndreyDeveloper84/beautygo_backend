"""Django Admin configuration for services app."""
from __future__ import annotations

from django import forms
from django.contrib import admin

from services.normalization import normalize_service_name

from .models import (
    DraftSalonService,
    ExternalBusyInterval,
    ExternalSourceMapping,
    GoalOption,
    GoalOptionCategory,
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
        'name', 'lifecycle', 'category', 'duration_default',
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
    search_fields = ('name', 'name_short', 'category__name', 'synonyms__text')
    list_editable = ('is_popular', 'sort_order')
    ordering = ('category', '-is_popular', 'sort_order', 'name')
    inlines = [RegionalPricingInline, ServiceTemplateSynonymInline]


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
    )
    # Фильтр по статусу — рабочий инструмент очереди проверки: «покажи
    # всё, что ждёт подтверждения». Индекс `(tenant, mapping_status)` в
    # модели заведён под эту выборку и до сих пор был никем не спрошен.
    list_filter = (
        'mapping_status', 'is_active', 'source', 'requires_health_check', 'tenant',
    )
    search_fields = ('name', 'tenant__slug', 'template__name')
    raw_id_fields = ('template', 'category')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'name')
    inlines = [SpecialistServiceInline]

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


@admin.register(GoalOption)
class GoalOptionAdmin(admin.ModelAdmin):
    list_display = ('key', 'label', 'sort_order', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('key', 'label')
    list_editable = ('sort_order', 'is_active')
    ordering = ('sort_order', 'key')
    inlines = [GoalOptionCategoryInline]


@admin.register(GoalOptionCategory)
class GoalOptionCategoryAdmin(admin.ModelAdmin):
    list_display = ('goal_option', 'category', 'sort_order')
    list_filter = ('goal_option',)
    search_fields = ('goal_option__key', 'goal_option__label', 'category__name')
    raw_id_fields = ('category',)
    ordering = ('goal_option', 'sort_order')
