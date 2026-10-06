"""Дневник питания пишется только под основанием ``food_diary_processing`` (DRF-2777).

Решение владельца 05.10 (D-2, DRF-2434): бьюти-инсайт и всё, что обрабатывает
дневник, держится на согласии ``food_diary_processing``. Значит, и сама запись
в дневник обязана идти под ним. До этого листа каталог согласие не проверял ни
на одном пути: бот-пути защищены ботом (M1, до вызова каталога), а клиентская
ручка ``/nutrition/food-log/`` — ничем.

### Две двери — два правила

Каталог **не хранит реестр согласий** (он в боте) — тот же довод, что у
:mod:`nutrition.services.personal_calculation_consent`. Но с DRF-2776 каталог
знает **последнее доставленное ботом состояние** по типу (``ConsentState``).

* **Клиентская ручка** (внешнее клиентское приложение каталога, бот её не
  зовёт) требует **утверждения основания**: вид согласия и версию текста, как
  у параметров тела. Без утверждения — отказ ``CONSENT_REQUIRED``, а не тихая
  запись. Известный отзыв побеждает и утверждение: человек отозвал — значит,
  запись под старым утверждением не идёт.
* **Внутренние пути бота** (``internal/food-log/``, восстановление записи,
  запись воды с её зеркалом в дневник) утверждения не требуют: согласие
  проверяет бот до вызова, и требование утверждения стало бы изменением двух
  репозиториев с риском порядка выкладки (каталог раньше бота — запись еды и
  воды в боте сломана). Здесь каталог ловит только **рассинхрон**: известный
  отзыв — отказ. Неизвестное состояние — не отзыв (решение (б) DRF-2776):
  основной сторож у этих путей — бот.

### Чего этот сторож не делает

* Не трогает уже лежащие записи: что делать с дневником при отзыве — не
  решено, D-2 говорит только «не обрабатывать для инсайта».
* Не закрывает правку существующей записи — это не новая запись в дневник;
  если владелец решит иначе, это одна строка в ручке правки.
"""

from __future__ import annotations

from typing import Any

from users.consent_events import FOOD_DIARY_PROCESSING
from users.models import ConsentState

#: Машинный код отказа — тот же, что у параметров тела: вызывающему важно
#: отличить «нет основания» (чинится согласием) от «данные невалидны».
CONSENT_REQUIRED = "CONSENT_REQUIRED"


class FoodDiaryConsentRequired(Exception):
    """Запись в дневник без основания ``food_diary_processing``.

    ``reason``: ``not_attested`` — клиент не утвердил основание;
    ``withdrawn`` — последнее доставленное ботом состояние — отзыв.
    """

    code = CONSENT_REQUIRED

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            "Запись в дневник питания принимается только под согласием "
            f"{FOOD_DIARY_PROCESSING!r} ({reason})"
        )


def is_known_withdrawn(user_id: int) -> bool:
    """Последнее доставленное ботом состояние ``food_diary_processing`` — отзыв.

    Тот же предикат, что у бьюти-инсайта
    (:func:`users.consent_events.food_diary_withdrawn_user_ids`): одна строка
    состояния на (человек, тип), и она уже упорядочена по ``granted_at``.
    """
    return ConsentState.objects.filter(
        user_id=user_id, consent_type=FOOD_DIARY_PROCESSING, granted=False,
    ).exists()


def require_not_withdrawn(user_id: int) -> None:
    """Внутренние пути бота: отказ только при известном отзыве."""
    if is_known_withdrawn(user_id):
        raise FoodDiaryConsentRequired("withdrawn")


def require_attested_basis(user_id: int, payload: Any) -> None:
    """Клиентская ручка: утверждение основания обязательно, отзыв побеждает.

    Fail-closed: отсутствие утверждения — отказ, а не разрешение. Версия
    обязательна, как у параметров тела: согласие без версии через полгода
    нельзя отличить от согласия на другой текст.
    """
    attestation = payload.get("consent") if hasattr(payload, "get") else None
    if not isinstance(attestation, dict):
        raise FoodDiaryConsentRequired("not_attested")
    if attestation.get("type") != FOOD_DIARY_PROCESSING:
        raise FoodDiaryConsentRequired("not_attested")
    version = attestation.get("document_version")
    if not isinstance(version, str) or not version.strip():
        raise FoodDiaryConsentRequired("not_attested")
    require_not_withdrawn(user_id)
