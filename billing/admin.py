from django.contrib import admin

from billing.models import (
    BillingConsent,
    BillingInvoice,
    BillingPayment,
    BookingFee,
    SpecialistSubscription,
    TariffPlan,
)


@admin.register(TariffPlan)
class TariffPlanAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'price', 'max_masters', 'is_active')
    list_filter = ('is_active',)


@admin.register(SpecialistSubscription)
class SpecialistSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'tenant', 'tariff', 'status', 'current_period_end')
    list_filter = ('status', 'tariff__code')
    raw_id_fields = ('user', 'tenant')


@admin.register(BookingFee)
class BookingFeeAdmin(admin.ModelAdmin):
    list_display = ('appointment', 'subscription', 'amount', 'period_start', 'status')
    list_filter = ('status', 'period_start')
    raw_id_fields = ('appointment', 'subscription', 'invoice')


@admin.register(BillingInvoice)
class BillingInvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'subscription', 'period_start', 'total_amount', 'status', 'paid_at')
    list_filter = ('status',)
    raw_id_fields = ('subscription',)


@admin.register(BillingPayment)
class BillingPaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'invoice', 'kind', 'amount', 'status', 'created_at')
    list_filter = ('kind', 'status')
    raw_id_fields = ('invoice',)


@admin.register(BillingConsent)
class BillingConsentAdmin(admin.ModelAdmin):
    list_display = ('user', 'document_version', 'given_at', 'revoked_at')
    raw_id_fields = ('user',)
