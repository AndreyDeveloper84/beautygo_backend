"""Read-only admin for the access journal, behind its own permission.

Owner §96: the journal is itself sensitive data, so being able to read it is
a separate grant — not something that rides along with "is staff" or with
access to the users table. And the journal is append-only, so the admin
offers no way to add, change or delete: not disabled by a checkbox, absent.

``is_superuser`` is deliberately NOT honoured as a bypass. DRF-1025 records
that the Django superuser has no tenant limits at all, and the salon-admin
surface already refuses to inherit that; the same reasoning applies with more
force here. Reading who accessed whose personal data is a grant somebody must
make on purpose — ``privacy_audit.view_personal_data_access_log``, granted
to a group.
"""
from __future__ import annotations

from django.contrib import admin

from privacy_audit.models import PersonalDataAccessLog

_PERM = "privacy_audit.view_personal_data_access_log"


@admin.register(PersonalDataAccessLog)
class PersonalDataAccessLogAdmin(admin.ModelAdmin):
    list_display = (
        "occurred_at", "operation", "result", "denial_reason",
        "object_id", "actor", "actor_role", "actor_named", "caller_purpose",
    )
    list_filter = (
        "result", "operation", "object_category", "denial_reason",
        "actor_named", "caller_purpose",
    )
    search_fields = ("object_id", "request_id", "basis")
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at",)

    # Every field, always read-only: the model refuses updates anyway, and a
    # form that offered them would just be a way to meet that refusal as a
    # 500 instead of as a closed door.
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_view_permission(self, request, obj=None) -> bool:
        return request.user.has_perm(_PERM)

    def has_module_permission(self, request) -> bool:
        return request.user.has_perm(_PERM)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
