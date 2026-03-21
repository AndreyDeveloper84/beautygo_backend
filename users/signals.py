from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, SpecialistProfile, User


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created and instance.role in ('client', 'specialist'):
        Profile.objects.create(user=instance)
    if created and instance.role == 'specialist':
        SpecialistProfile.objects.create(
            user=instance, display_name=instance.username,
        )
