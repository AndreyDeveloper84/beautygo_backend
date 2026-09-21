"""DRF-2214 — «забудь всё» в каталоге стирает то, что запомнено о человеке.

До этого листа оба пути «забудь всё» (бот — ``internal_personal_context_api``,
приложение — ``personal_context_views``) звали только
:func:`users.personal_context_erasure.erase_personal_context` — одну строку
``UserPersonalContext``. Цель с дословным ``goal_text``, анкета цели со
свободными ответами, план и wellness вокруг него, профиль питания с весом,
ростом и ``health_flags`` оставались лежать. Удаление аккаунта (D3,
:func:`users.deletion_executor._erase_catalog`) всё это стирает; «забудь всё»
отставало именно от него.

# Договор с человеком — текст команды

Бот говорит: «я забуду всё, что запомнила о тебе из наших разговоров, и анкету
предпочтений», а остаются ТОЛЬКО «бронирования и оплаты (это по закону) и
настройки уведомлений с датой рождения». Цель, анкета цели, план и профиль
питания в список остающегося не входят — значит, уходят.

# Почему отдельная функция, а не вызов D3

D3 — удаление аккаунта: он обезличивает записи на приём (``client`` →
надгробие, ``notes`` → ""), стирает диалоги, уведомления, токены устройств. Для
«забудь всё» это было бы ложью в обратную сторону — человек остаётся клиентом
салона, запись по-прежнему его, аккаунт живёт. Поэтому здесь ровно шаги D3 по
целям, wellness и профилю питания — и ничего больше. Шаг целей и wellness идёт
строка в строку в порядке D3 (шаг 3 ``_erase_catalog``), потому что тот учитывает
``PROTECT`` внутри набора человека. Профиль питания у D3 стирается первым, здесь —
вторым: связей между двумя группами нет, порядок между ними безразличен.

# Известный пробел — до PR-3

``recommendation.Recommendation.target_outcomes`` держит UUID исходов
(``DesiredOutcome``); D3 чистит их через ``anonymise_for_subject``, здесь они
остаются указывать на стёртые строки. Не падает и сам UUID — не персональные
данные, но это расхождение с D3; закрывается в PR-3 вместе с рекомендациями.

# Дневник — не здесь

``FoodLog``, ``WaterEntry``, ``SavedMeal``, ``FoodScan`` с фото — вопрос к
владельцу: по букве текста они тоже уходят, но человек вносил их сам и может не
ждать, что пропадут вместе с «разговорами». Добавляются отдельным коммитом по
ответу.

# Что вызывающий обязан

Звать в одной транзакции с ``erase_personal_context`` — либо стёрто всё, либо
ничего. Форму ответа бота и ``scope`` события ``personal_data_deleted`` эта
функция не трогает: и ответ, и событие разбирают потребители, и новый состав
им сообщается отдельно (DRF-2214 PR-3), а не тихой сменой формы.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("users.forget_all.catalog")


def erase_remembered_catalog(user, *, initiator: str) -> dict[str, int]:
    """Стереть цели, анкету цели, план и wellness, профиль питания. Идемпотентно.

    Возвращает счёт снятых строк по моделям — для журнала; значения не
    выводятся нигде (AMD-010: аудит без персональных данных).
    """
    from goals.models import ClientGoal, GoalAnketaRun
    from nutrition.models import NutritionProfile
    from nutrition.services.personal_calculation_withdrawal import (
        erase_personal_calculation_inputs,
    )
    from wellness.models import (
        DesiredOutcome,
        PersonalPlan,
        PlanAction,
        PlanOutcomeLink,
        ProgressObservation,
    )

    deleted: dict[str, int] = {}

    def _delete(key: str, qs) -> None:
        n, _ = qs.delete()
        deleted[key] = deleted.get(key, 0) + n

    # 1. Цели и планы — порядок D3: связи план↔цель первыми, затем
    #    обязательства плана (PROTECT на PlanAction.plan), сам план, исходы;
    #    у наблюдений PROTECT на superseded_by внутри набора — снять указатели,
    #    потом строки. GoalAnketaAnswer (свободный answer_text) уходит каскадом
    #    вместе с прогоном анкеты.
    _delete("wellness.PlanOutcomeLink", PlanOutcomeLink.objects.filter(plan__user=user))
    _delete("wellness.PlanOutcomeLink", PlanOutcomeLink.objects.filter(outcome__user=user))
    _delete("wellness.PlanAction", PlanAction.objects.filter(plan__user=user))
    _delete("wellness.PersonalPlan", PersonalPlan.objects.filter(user=user))
    _delete("wellness.DesiredOutcome", DesiredOutcome.objects.filter(user=user))
    ProgressObservation.objects.filter(user=user).update(superseded_by=None)
    _delete("wellness.ProgressObservation", ProgressObservation.objects.filter(user=user))
    _delete("goals.GoalAnketaRun", GoalAnketaRun.objects.filter(client=user))
    _delete("goals.ClientGoal", ClientGoal.objects.filter(client=user))

    # 2. Профиль питания — через канонического писателя (тот же, что у
    #    отзыва согласия §2 и у D3): ориентиры и входы стираются им, а не
    #    своим удалением, затем сама строка.
    erase_personal_calculation_inputs(user)
    _delete("nutrition.NutritionProfile", NutritionProfile.objects.filter(user=user))

    logger.info(
        "forget_all.catalog.erased user=%s initiator=%s counts=%s",
        user.pk,
        initiator,
        {k: v for k, v in deleted.items() if v},
    )
    return deleted
