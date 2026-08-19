"""Клиентская цель (Transformation Goal) — durable-факт выбора.

DRF-1190 / OD-1 (2026-08-19): цель — гибрид: курируемый ключ (`goal_key`)
из списка подсказок ИЛИ свободный текст (`goal_text`), сохраняемый
дословно — он является будущим корпусом формулировок (OD-2: корпуса нет,
пилот — механизм его сбора).

`ClientGoal` — факт («клиент выбрал/написал цель в момент времени»),
а НЕ проекция понимания. Эфемерный документ состояния (DecisionContext)
строится поверх этой таблицы и в БД не хранится — см. Ответ 3 главного
окна в docs/REPLY_CONVERSATION_ARCH.md: не смешивать их в одну таблицу.

Инварианты:
- хотя бы одно из `goal_key` / `goal_text` заполнено (CheckConstraint);
- одна активная цель на клиента (partial UniqueConstraint): смена цели
  закрывает прежний ряд (`is_active=False`) и создаёт новый — закрытые
  ряды и есть история, отдельного журнала не заводим.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ClientGoal(models.Model):
    """Выбранная клиентом цель — единственная активная на клиента."""

    class SourceChannel(models.TextChoices):
        BOT = "bot", "Бот (DM)"
        MINIAPP = "miniapp", "Mini App"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="client_goals",
    )
    # Ключ курируемой подсказки (services.GoalOption.key). NULL — цель
    # сформулирована свободным текстом и пока не отображена на ключ
    # (низкая уверенность -> уточнение, а не насильный маппинг; OD-1).
    goal_key = models.SlugField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Ключ курируемой цели (GoalOption.key); NULL при свободном вводе",
    )
    # Дословная формулировка пользователя. Хранится даже при распознанном
    # ключе — это будущий датасет формулировок (OD-2).
    goal_text = models.TextField(
        null=True,
        blank=True,
        help_text="Дословный свободный ввод пользователя; не нормализуется",
    )
    selected_at = models.DateTimeField(default=timezone.now)
    source_channel = models.CharField(
        max_length=16,
        choices=SourceChannel.choices,
        help_text="Канал, в котором цель выбрана (бот ↔ Mini App; цель переживает смену канала)",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-selected_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(goal_key__isnull=False)
                    | models.Q(goal_text__isnull=False)
                ),
                name="clientgoal_key_or_text_present",
            ),
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(is_active=True),
                name="clientgoal_one_active_per_client",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "is_active"],
                name="clientgoal_client_active_idx",
            ),
        ]

    def __str__(self) -> str:
        shown = self.goal_key or (self.goal_text or "")[:40]
        return f"ClientGoal<{self.client_id}> {shown} (active={self.is_active})"
