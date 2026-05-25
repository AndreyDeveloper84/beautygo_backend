from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, SpecialistProfile, TenantUserRelationship, User


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created and instance.role in ('client', 'specialist'):
        Profile.objects.create(user=instance)
    if created and instance.role == 'specialist':
        SpecialistProfile.objects.create(
            user=instance, display_name=instance.username,
        )


# Role mapping mirrors users/migrations/0012's backfill — keep both
# definitions in sync if/when new User.role values land.
_USER_ROLE_TO_TUR_ROLE = {
    'client': TenantUserRelationship.Role.CUSTOMER,
    'specialist': TenantUserRelationship.Role.STAFF,
    'admin': TenantUserRelationship.Role.ADMIN,
}


@receiver(post_save, sender=User)
def ensure_tenant_user_relationship(sender, instance, **kwargs):
    """#246 sub-phase 1.B bridge: legacy code sets ``user.tenant=X;
    user.save()`` to scope a user to a tenant. Until that pattern is
    fully migrated to explicit TUR creation (Sprint 2 cleanup), this
    signal mirrors it — when `user.tenant_id` is set, ensure an active
    TUR row exists for (user, user.tenant).

    Idempotent via get_or_create; the partial unique constraint
    catches duplicates. ``granted_by='self'`` because the User.save()
    caller is what asserted the tenant binding; if you need
    ``granted_by='system'`` (backfill) or 'admin' (admin-initiated),
    create the TUR explicitly instead of relying on this bridge.

    Setting ``user.tenant=None`` does NOT revoke existing TURs — revoke
    is an explicit action (separate management command / endpoint),
    not a side effect of clearing the FK.
    """
    if not instance.tenant_id:
        return

    # F2 defense (adversarial review PR #152): if a revoked TUR
    # exists for this (user, tenant) pair, do NOT silently re-grant.
    # Revoke is an admin-gated action — bypassing via a side-effect of
    # `user.save()` would be a security hole. Re-grant requires
    # explicit `TUR.objects.create(...)` or an admin endpoint.
    has_revoked = TenantUserRelationship.objects.filter(
        user=instance,
        tenant_id=instance.tenant_id,
        is_active=False,
    ).exists()
    if has_revoked:
        import logging
        logging.getLogger(__name__).warning(
            "User.tenant bridge skipped: revoked TUR exists for "
            "user=%s tenant=%s. Re-grant requires explicit admin "
            "action (separate endpoint / management command).",
            instance.id, instance.tenant_id,
        )
        return

    tur_role = _USER_ROLE_TO_TUR_ROLE.get(
        instance.role, TenantUserRelationship.Role.CUSTOMER,
    )
    TenantUserRelationship.objects.get_or_create(
        user=instance,
        tenant_id=instance.tenant_id,
        is_active=True,
        defaults={
            'role': tur_role,
            'granted_by': TenantUserRelationship.GrantedBy.SELF,
        },
    )
