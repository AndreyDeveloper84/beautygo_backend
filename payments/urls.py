from django.urls import path

from .views import (
    PaymentCreateView,
    PaymentDetailView,
    PaymentRefundView,
    PaymentRetryView,
    PaymentWebhookView,
)

urlpatterns = [
    path('create/', PaymentCreateView.as_view(), name='payment-create'),
    path('webhook/', PaymentWebhookView.as_view(), name='payment-webhook'),
    path('<uuid:pk>/', PaymentDetailView.as_view(), name='payment-detail'),
    path('<uuid:pk>/retry/', PaymentRetryView.as_view(), name='payment-retry'),
    path('<uuid:pk>/refund/', PaymentRefundView.as_view(), name='payment-refund'),
]
