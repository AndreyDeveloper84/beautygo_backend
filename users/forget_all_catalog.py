"""DRF-2214 — «забудь всё» в каталоге стирает то, что запомнено о человеке.

До этого листа все пути «забудь всё» звали только
:func:`users.personal_context_erasure.erase_personal_context` — одну строку
``UserPersonalContext``. Пути три, и зовут эту функцию все:

- **бот** — C5.2 ``users.personal_data_api.InternalPersonalDataDeleteView``
  (ai-bot-platform ``apps/identity/services/personal_context.py`` — «The ONE
  erase verb»); эту функцию он зовёт для каждой личности субъекта, а
  ``erase_personal_context`` — для аккаунта и прокси со строкой профиля. Его же
  зовут задание повтора DRF-1950, отзыв согласия на хранение, удаление
  аккаунта и мини-апп;
- **приложение** — ``users.personal_context_views``;
- ``users.internal_personal_context_api`` (``DELETE …/personal-context/``) —
  живой эндпоинт, но бот его НЕ зовёт. #526 ошибочно назвал его путём бота и
  правил только его и приложение; C5.2 подключён отдельным изменением
  (DRF-2214, вместе с дневником).
 Цель с дословным ``goal_text``, анкета цели со
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

# Дневник — по слову владельца (CURRENT_DECISIONS §66)

«а — стирать дневник вместе со всем». ``FoodLog``, ``FoodScan`` с фото,
``WaterEntry`` и ``SavedMeal`` (с мягко удалёнными), и соседи из того же шага D3:
``DeletedFoodLog`` (снимок на окно восстановления — иначе «восстановить» вернёт
стёртое), ``WaterLog`` (старый дневник воды), ``ProfileIdempotencyKey``
(суточный кэш ответа с профилем питания), ``CrossDomainShownRule`` (история
показанных подсказок). Шаг — строка в строку шаг 2 D3.

Фото сканера снимаются с носителя РАНЬШЕ строк, внутри транзакции, — как у D3.
Откат оставляет строку без файла: повтор найдёт её и дочистит (отсутствующий
файл — не ошибка). Файл без строки — а его дало бы удаление после коммита,
упавшее уже после ответа «стёрто», — не нашёл бы никто.

# Что вызывающий обязан

Звать в одной транзакции с ``erase_personal_context`` — либо стёрто всё, либо
ничего — и передать ему ``also_erased=remembered_scope(counts)``: журнал AMD-010
и ответ называют стёртое закрытым словарём :data:`REMEMBERED_SCOPE`. Без этого
стёртые цели и дневник за уже стёртым профилем журнал звал «нечего было
стирать» (DRF-2214). У личности без строки профиля (связанный прокси)
вызывающий пишет событие сам, если :func:`remembered_scope` не пуст.

C5.3 ``erasure-status`` считает остаток той же меркой —
:func:`remembered_residue`, только число.
"""
from __future__ import annotations

import logging

from django.db.models import Q

logger = logging.getLogger("users.forget_all.catalog")

#: DRF-2226 — что стирает удаление аккаунта (D3, ``_erase_catalog``) и НЕ стирает
#: «забудь всё», с причиной. Сторож ``users/tests/test_forget_all_vs_deletion_census_2226``
#: держит список ТОЧНЫМ: модель, добавленная в D3 и не внесённая ни в «забудь
#: всё», ни сюда, — красная; лишняя запись здесь — тоже. Причины — из договора
#: с человеком выше («остаются бронирования и оплаты (это по закону) и
#: настройки уведомлений»); где договор неоднозначен, это названо вопросом
#: владельцу, а не решено молча.
KEPT_BY_FORGET_ALL: dict[str, str] = {
    "users.SocialAccount": (
        "аккаунт живёт после «забудь всё»: вход через соцсеть — сам аккаунт, "
        "не запомненное о человеке"
    ),
    "users.DeviceToken": (
        "аккаунт живёт: токен устройства — доставка уведомлений, настройки "
        "уведомлений по договору остаются"
    ),
    "users.AnonymousSession": (
        "аккаунт живёт: гостевая сессия — механика входа, не запомненное из разговоров"
    ),
    "payments.UserPaymentMethod": (
        "«оплаты — по закону» остаются: способ оплаты принадлежит аккаунту и его сделкам"
    ),
    "appointments.IdempotencyKey": (
        "«бронирования — по закону» остаются: ключ повтора создания записи — часть сделки"
    ),
    "users.SpecialistPortfolio": (
        "работы мастера — его профессиональный профиль в каталоге, а не запомненное о клиенте"
    ),
    "notifications.Notification": (
        "вопрос владельцу: договор оставляет «настройки уведомлений», про историю "
        "отправленных молчит; до решения — остаётся"
    ),
    "users.FavoriteSpecialist": (
        "вопрос владельцу: избранные мастера — выбор человека в каталоге, в договор "
        "«забудь всё» не названы; до решения — остаются"
    ),
    "ai.Conversation": (
        "вопрос владельцу: договор бота «забуду … из наших разговоров» про память бота; "
        "ИИ-чат приложения (каталог) им не назван; до решения — остаётся"
    ),
}

#: Ключ счёта :func:`erase_remembered_catalog` → слово журнала AMD-010 и ответа.
#: Закрытый словарь: сторож (``users/tests/test_erasure_truth_2214.py``) держит,
#: что каждый ключ, который функция возвращает, здесь назван.
REMEMBERED_SCOPE: dict[str, str] = {
    "goals.ClientGoal": "goals",
    "goals.GoalAnketaRun": "goals",
    "wellness.PlanOutcomeLink": "wellness_plan",
    "wellness.PlanAction": "wellness_plan",
    "wellness.PersonalPlan": "wellness_plan",
    "wellness.DesiredOutcome": "wellness_plan",
    "wellness.ProgressObservation": "wellness_plan",
    "nutrition.NutritionProfile": "nutrition_profile",
    # Суточный кэш ответа с профилем питания внутри — это профиль.
    "nutrition.ProfileIdempotencyKey": "nutrition_profile",
    "nutrition.FoodScan": "food_diary",
    "nutrition.FoodLog": "food_diary",
    "nutrition.DeletedFoodLog": "food_diary",
    "nutrition.WaterEntry": "food_diary",
    "nutrition.WaterLog": "food_diary",
    "nutrition.SavedMeal": "food_diary",
    "files": "food_diary",
    "nutrition.CrossDomainShownRule": "shown_hints",
}

#: Порядок слов в scope — стабильный, от целей к дневнику.
SCOPE_ORDER: tuple[str, ...] = (
    "goals", "wellness_plan", "nutrition_profile", "food_diary", "shown_hints",
)


def remembered_scope(counts: dict[str, int]) -> list[str]:
    """Слова журнала для того, что :func:`erase_remembered_catalog` сняло.

    Только группы, где снято хоть что-то: повтор — пустой список, как у
    профиля (контракт идемпотентности C5.2).
    """
    hit = {REMEMBERED_SCOPE[key] for key, n in counts.items() if n}
    return [word for word in SCOPE_ORDER if word in hit]


def _remembered_querysets(user) -> dict:
    """Строки, которые стирает :func:`erase_remembered_catalog`, — для счёта остатка."""
    from goals.models import ClientGoal, GoalAnketaRun
    from nutrition.models import (
        CrossDomainShownRule,
        DeletedFoodLog,
        FoodLog,
        FoodScan,
        NutritionProfile,
        ProfileIdempotencyKey,
        SavedMeal,
        WaterEntry,
        WaterLog,
    )
    from wellness.models import (
        DesiredOutcome,
        PersonalPlan,
        PlanAction,
        PlanOutcomeLink,
        ProgressObservation,
    )

    return {
        "goals.ClientGoal": ClientGoal.objects.filter(client=user),
        "goals.GoalAnketaRun": GoalAnketaRun.objects.filter(client=user),
        "wellness.PlanOutcomeLink": PlanOutcomeLink.objects.filter(
            Q(plan__user=user) | Q(outcome__user=user)
        ),
        "wellness.PlanAction": PlanAction.objects.filter(plan__user=user),
        "wellness.PersonalPlan": PersonalPlan.objects.filter(user=user),
        "wellness.DesiredOutcome": DesiredOutcome.objects.filter(user=user),
        "wellness.ProgressObservation": ProgressObservation.objects.filter(user=user),
        "nutrition.NutritionProfile": NutritionProfile.objects.filter(user=user),
        "nutrition.ProfileIdempotencyKey": ProfileIdempotencyKey.objects.filter(user=user),
        "nutrition.FoodScan": FoodScan.objects.filter(user=user),
        "nutrition.FoodLog": FoodLog.objects.filter(user=user),
        "nutrition.DeletedFoodLog": DeletedFoodLog.objects.filter(user=user),
        "nutrition.WaterEntry": WaterEntry.objects.filter(user=user),
        "nutrition.WaterLog": WaterLog.objects.filter(user=user),
        "nutrition.SavedMeal": SavedMeal.objects.filter(user=user),
        "nutrition.CrossDomainShownRule": CrossDomainShownRule.objects.filter(user=user),
    }


def remembered_residue(user) -> int:
    """Сколько запомненных строк у ``user`` осталось — только число (C5.3).

    Ничего не создаёт и значений не читает: ``count()`` по тем же наборам,
    что стирает :func:`erase_remembered_catalog`.
    """
    return sum(qs.count() for qs in _remembered_querysets(user).values())


def erase_remembered_catalog(
    user, *, initiator: str, removed_files: set[str] | None = None
) -> dict[str, int]:
    """Стереть цели, анкету цели, план и wellness, профиль питания, дневник с фото.

    Идемпотентно. Файл фото, который не снялся с носителя, — ``IncompleteErasure``
    (откат у вызывающего, 500), отсутствующий — не ошибка.

    Возвращает счёт снятых строк по моделям — для журнала; значения не
    выводятся нигде (AMD-010: аудит без персональных данных).
    """
    from goals.models import ClientGoal, GoalAnketaRun
    from nutrition.models import (
        CrossDomainShownRule,
        DeletedFoodLog,
        FoodLog,
        FoodScan,
        NutritionProfile,
        ProfileIdempotencyKey,
        SavedMeal,
        WaterEntry,
        WaterLog,
    )
    from nutrition.services.personal_calculation_withdrawal import (
        erase_personal_calculation_inputs,
    )
    from users.deletion_executor import _delete_file
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

    # 3. Дневник (§66) — шаг 2 D3: файлы сканов раньше строк (см. докстринг
    #    модуля), затем строки; избранные блюда — вместе с мягко удалёнными.
    #    DRF-2256: вызывающий мог снять файлы пачкой ДО транзакции
    #    (``users.scan_file_erasure``) и передать их имена — тогда здесь
    #    снимаются только файлы сканов, появившихся после пачки. Счёт
    #    ``files`` — только снятые здесь: пачка не говорит, какие файлы
    #    существовали (отсутствующий ключ S3 — не ошибка).
    files_deleted = 0
    for scan in FoodScan.objects.filter(user=user).only("id", "image"):
        if removed_files is not None and scan.image.name in removed_files:
            continue
        files_deleted += _delete_file(scan.image)
    _delete("nutrition.FoodScan", FoodScan.objects.filter(user=user))
    _delete("nutrition.FoodLog", FoodLog.objects.filter(user=user))
    _delete("nutrition.DeletedFoodLog", DeletedFoodLog.objects.filter(user=user))
    _delete("nutrition.WaterEntry", WaterEntry.objects.filter(user=user))
    _delete("nutrition.WaterLog", WaterLog.objects.filter(user=user))
    _delete("nutrition.CrossDomainShownRule", CrossDomainShownRule.objects.filter(user=user))
    _delete("nutrition.ProfileIdempotencyKey", ProfileIdempotencyKey.objects.filter(user=user))
    _delete("nutrition.SavedMeal", SavedMeal.objects.filter(user=user))
    deleted["files"] = files_deleted

    logger.info(
        "forget_all.catalog.erased user=%s initiator=%s counts=%s",
        user.pk,
        initiator,
        {k: v for k, v in deleted.items() if v},
    )
    return deleted
