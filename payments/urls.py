from django.urls import path

from .views import (
    InternalPaymentRetryView,
    PaymentCreateView,
    PaymentDetailView,
    PaymentRefundView,
    PaymentRetryView,
    PaymentWebhookView,
)

urlpatterns = [
    path('create/', PaymentCreateView.as_view(), name='payment-create'),
    path('webhook/', PaymentWebhookView.as_view(), name='payment-webhook'),
    # Internal/bot endpoints — Bearer + X-External-User-ID auth. The
    # `internal/` prefix sits BEFORE the <uuid:pk>/ route so the
    # resolver doesn't try to coerce the literal "internal" into a UUID.
    path(
        'internal/<uuid:pk>/retry/',
        InternalPaymentRetryView.as_view(),
        name='payment-internal-retry',
    ),
    path('<uuid:pk>/', PaymentDetailView.as_view(), name='payment-detail'),
    path('<uuid:pk>/retry/', PaymentRetryView.as_view(), name='payment-retry'),
    path('<uuid:pk>/refund/', PaymentRefundView.as_view(), name='payment-refund'),
]
