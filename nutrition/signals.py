"""Сброс AI-комментария к дню при любом изменении дневника (DRF-2227).

Сигналы на моделях, а не вызовы в сервисах: запись еды и воды приходит
многими путями (ручной ввод, скан, сохранённое блюдо, восстановление из
корзины, трекер напитков, админка), и сервисный вызов, забытый в одном из
них, вернул бы старый комментарий к новым цифрам на шесть часов.
``QuerySet.delete()`` тоже доходит сюда: Django шлёт ``post_delete`` на каждую
строку, когда у модели есть получатель.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from nutrition.models import FoodLog, WaterEntry, WaterLog
from nutrition.services.ai_comment_service import invalidate_comment_cache


@receiver(post_save, sender=FoodLog, dispatch_uid="ai_comment_reset_foodlog_save")
@receiver(post_delete, sender=FoodLog, dispatch_uid="ai_comment_reset_foodlog_delete")
@receiver(post_save, sender=WaterEntry, dispatch_uid="ai_comment_reset_waterentry_save")
@receiver(post_delete, sender=WaterEntry, dispatch_uid="ai_comment_reset_waterentry_delete")
@receiver(post_save, sender=WaterLog, dispatch_uid="ai_comment_reset_waterlog_save")
@receiver(post_delete, sender=WaterLog, dispatch_uid="ai_comment_reset_waterlog_delete")
def _reset_ai_comment(sender, instance, **kwargs) -> None:
    user_id = getattr(instance, "user_id", None)
    if user_id is not None:
        invalidate_comment_cache(user_id)
