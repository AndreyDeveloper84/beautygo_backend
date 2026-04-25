from django.urls import path

from .devices_api import DeviceDetailView, DeviceRegisterView

urlpatterns = [
    path("register/", DeviceRegisterView.as_view(), name="devices-register"),
    path("<uuid:pk>/", DeviceDetailView.as_view(), name="devices-detail"),
]
