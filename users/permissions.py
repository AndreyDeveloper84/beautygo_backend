"""Custom permission classes for BeautyGO API."""

from rest_framework import permissions


class IsClientApp(permissions.BasePermission):
    """Allow only requests from BeautyGO client app (X-App-Type: client)."""
    message = "Этот эндпоинт доступен только из приложения BeautyGO"

    def has_permission(self, request, view):
        return getattr(request, 'app_type', None) == 'client'


class IsProApp(permissions.BasePermission):
    """Allow only requests from BeautyGO Pro app (X-App-Type: pro)."""
    message = "Этот эндпоинт доступен только из приложения BeautyGO Pro"

    def has_permission(self, request, view):
        return getattr(request, 'app_type', None) == 'pro'


class IsClient(permissions.BasePermission):
    """Allow access only to users with role=client."""

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.role == 'client'
        )


class IsSpecialist(permissions.BasePermission):
    """Allow access only to users with role=specialist."""

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.role == 'specialist'
        )
