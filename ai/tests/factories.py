"""Test factories for AI Chat models.

Plain functions over factory_boy here — the project mixes both styles
and these objects are simple enough that a function is clearer.
"""
from __future__ import annotations

import uuid
from typing import Any

from ai.models import Conversation, Message
from users.models import Profile, SpecialistProfile, User


_user_counter = 0


def _next_phone() -> str:
    global _user_counter
    _user_counter += 1
    return f"+7999{_user_counter:07d}"


def make_user(
    *,
    role: str = "client",
    is_guest: bool = False,
    city: str = "Penza",
    first_name: str = "Тест",
    **extra: Any,
) -> User:
    user = User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        password="x",
        role=role,
        phone=_next_phone(),
        is_guest=is_guest,
        first_name=first_name,
        **extra,
    )
    # User post_save signal already creates a Profile/SpecialistProfile.
    # Just patch in the city field for clients.
    if role == "client" and not is_guest:
        Profile.objects.filter(user=user).update(
            full_name=first_name, city=city,
        )
    return user


def make_specialist(
    *,
    display_name: str = "Мастер Тест",
    rating: float = 4.8,
    reviews_count: int = 25,
    address: str = "Penza, Lenina 1",
    is_available: bool = True,
    is_booking_enabled: bool = True,
    status: str = SpecialistProfile.ProfileStatus.ACTIVE,
) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"s-{uuid.uuid4().hex[:8]}",
        password="x",
        role="specialist",
        phone=_next_phone(),
    )
    # Signal post_save creates SpecialistProfile when role=specialist —
    # update its fields rather than creating a duplicate.
    SpecialistProfile.objects.filter(user=user).update(
        display_name=display_name,
        rating=rating,
        reviews_count=reviews_count,
        address=address,
        is_available=is_available,
        is_booking_enabled=is_booking_enabled,
        status=status,
    )
    return SpecialistProfile.objects.get(user=user)


def make_conversation(*, user: User, is_active: bool = True) -> Conversation:
    return Conversation.objects.create(user=user, is_active=is_active)


def make_message(
    *,
    conversation: Conversation,
    role: str = Message.Role.USER,
    content: str = "Привет",
    action_type: str = "",
    action_data: dict | None = None,
) -> Message:
    return Message.objects.create(
        conversation=conversation,
        role=role,
        content=content,
        action_type=action_type,
        action_data=action_data,
    )
