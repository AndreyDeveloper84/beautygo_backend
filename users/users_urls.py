"""
/api/v1/users/ — spec-compliant aliases for user profile endpoints.

These mirror the Notion API spec paths. The original /auth/ paths remain
for backwards compatibility.
"""
from django.urls import path

from .views import ClientProfileView, UserMeView

urlpatterns = [
    # GET/DELETE /api/v1/users/me/
    path('me/', UserMeView.as_view(), name='users-me'),
    # GET/PATCH /api/v1/users/me/client-profile/
    path('me/client-profile/', ClientProfileView.as_view(), name='users-client-profile'),
]
