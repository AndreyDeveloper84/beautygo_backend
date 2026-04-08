import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.utils import timezone


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ROLE_CHOICES = (
        ('client', 'Client'),
        ('specialist', 'Specialist'),
        ('admin', 'Admin'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    is_guest = models.BooleanField(
        default=False,
        help_text="Anonymous (guest) user created on first app launch without registration",
    )
    onboarding_completed = models.BooleanField(
        default=False,
        help_text="User has completed the onboarding flow (name + location saved)",
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.username} ({self.role})"

    @property
    def is_client(self) -> bool:
        return self.role == 'client'

    @property
    def is_specialist(self) -> bool:
        return self.role == 'specialist'

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'


class Profile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )

    full_name = models.CharField(max_length=255)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    bio = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    experience_years = models.PositiveIntegerField(default=0)
    default_location_lat = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )
    default_location_lng = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )

    def __str__(self):
        return f"Профиль {self.user.username}"


class SpecialistProfile(models.Model):
    """Profile for specialists (masters) in BeautyGO Pro."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class ProfileStatus(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PENDING = "pending", "На верификации"
        ACTIVE = "active", "Активен"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="specialist_profile",
    )
    display_name = models.CharField(max_length=255)
    avatar = models.ImageField(
        upload_to='specialists/avatars/', blank=True, null=True,
    )
    bio = models.TextField(blank=True)
    experience_years = models.PositiveIntegerField(default=0)

    # Location
    address = models.CharField(max_length=500, blank=True)
    location_lat = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )
    location_lng = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )

    # Booking engine fields
    timezone = models.CharField(
        max_length=50, default="Europe/Moscow",
        help_text="IANA timezone string, e.g. Europe/Moscow",
    )
    is_booking_enabled = models.BooleanField(
        default=True,
        help_text="Master can pause bookings without deactivating profile",
    )

    # Status & ratings
    status = models.CharField(
        max_length=10,
        choices=ProfileStatus.choices,
        default=ProfileStatus.DRAFT,
    )
    rating = models.DecimalField(
        max_digits=2, decimal_places=1, default=0.0,
    )
    reviews_count = models.PositiveIntegerField(default=0)
    is_available = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.display_name} ({self.get_status_display()})"


class OTPCode(models.Model):
    """OTP code for phone verification."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone = models.CharField(max_length=20, db_index=True)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['phone', 'is_used', 'expires_at']),
        ]

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_used and not self.is_expired and self.attempts < settings.OTP_MAX_ATTEMPTS

    def __str__(self):
        return f"OTP for {self.phone} ({'used' if self.is_used else 'active'})"


class SocialAccount(models.Model):
    """Linked social provider account for a user."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Provider(models.TextChoices):
        VK = "vk", "VKontakte"
        GOOGLE = "google", "Google"
        APPLE = "apple", "Apple"
        YANDEX = "yandex", "Yandex"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='social_accounts',
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    provider_uid = models.CharField(max_length=255)
    extra_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('provider', 'provider_uid')
        indexes = [
            models.Index(fields=['user', 'provider']),
        ]

    def __str__(self):
        return f"{self.user.username} — {self.provider} ({self.provider_uid})"


class DeviceToken(models.Model):
    """FCM device token for push notifications."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class AppType(models.TextChoices):
        CLIENT = "client", "BeautyGO"
        PRO = "pro", "BeautyGO Pro"

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='device_tokens',
    )
    token = models.CharField(max_length=255, unique=True)
    app_type = models.CharField(max_length=10, choices=AppType.choices)
    platform = models.CharField(max_length=10, choices=Platform.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'app_type', 'is_active']),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.app_type}/{self.platform})"


class AnonymousSession(models.Model):
    """
    Tracks anonymous (guest) JWT sessions created before registration.

    One session per device_id. When the user signs up/logs in and provides
    the anonymous_token, the session is merged into the real account.
    """

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_id = models.UUIDField(unique=True, db_index=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='anonymous_session',
    )
    platform = models.CharField(max_length=10, choices=Platform.choices)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Anonymous Session'
        verbose_name_plural = 'Anonymous Sessions'
        indexes = [
            models.Index(fields=['device_id']),
            models.Index(fields=['expires_at']),
        ]

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"AnonSession device={self.device_id} platform={self.platform}"
