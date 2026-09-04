"""Internal bot-service API for the goal layer (DRF-1190).

Surface under /api/v1/internal/me/ — same auth pattern as the #97/#99
endpoints: Bearer service token + X-External-User-ID resolved into
``request.user`` by ``IsBotServiceWithVerifiedClient``.

- ``GET  decision-context/`` — эфемерный документ состояния (сердце
  проекции: экран-отрисовщик ничего не вычисляет сам).
- ``POST goals/select/`` — зафиксировать выбор (goal_key XOR goal_text)
  или намерение ``need_guidance``; ответ — обновлённый документ
  состояния (цикл проекции: уточнение — не тупик, а перерисовка).
"""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from core.errors import ErrorCode
from users.permissions import IsBotServiceWithVerifiedClient
from users.response import error_response, success_response

from . import anketa
from .decision_context import (
    INTENT_NEED_GUIDANCE,
    INTENT_START_ANKETA,
    build_decision_context,
    next_anketa_step,
    open_anketa_run,
)
from .models import ClientGoal, GoalAnketaAnswer, GoalAnketaRun

logger = logging.getLogger(__name__)


class AnketaAnswerSerializer(serializers.Serializer):
    """Ответ на один шаг анкеты: ``{step, option_key}`` или ``{step, text}``.

    ``step`` — эхо того, что прислал сервер. Он здесь не для того, чтобы
    клиент выбирал шаг (выбрать он не может: сервер ниже сверяет эхо с
    тем шагом, который сам ждёт), а чтобы ответ, разошедшийся с
    состоянием, был отвергнут явно, а не записан не в ту графу.
    """

    step = serializers.SlugField(max_length=32)
    option_key = serializers.SlugField(max_length=64, required=False)
    text = serializers.CharField(max_length=1000, required=False, allow_blank=False)

    def validate(self, attrs):
        if bool(attrs.get("option_key")) == bool(attrs.get("text")):
            raise serializers.ValidationError(
                "Provide exactly one of: option_key, text."
            )
        if not anketa.is_answerable_step(attrs["step"]):
            raise serializers.ValidationError({"step": "Unknown anketa step."})
        return attrs


class GoalSelectSerializer(serializers.Serializer):
    """Ровно одно из: goal_key / goal_text / intent / answer.

    DRF-1451 добавил четвёртый вариант (``answer`` — шаг анкеты) и
    второе намерение (``start_anketa`` — повторный проход, DRF-1225).
    Прежние три варианта не тронуты: анкета не отменяет их, она встаёт
    рядом (поправка A-1 к BOT-001, §24).
    """

    goal_key = serializers.SlugField(max_length=64, required=False)
    goal_text = serializers.CharField(max_length=1000, required=False, allow_blank=False)
    intent = serializers.ChoiceField(
        choices=[INTENT_NEED_GUIDANCE, INTENT_START_ANKETA], required=False,
    )
    answer = AnketaAnswerSerializer(required=False)
    source_channel = serializers.ChoiceField(
        choices=ClientGoal.SourceChannel.choices, required=True,
    )

    def validate(self, attrs):
        provided = [
            bool(attrs.get("goal_key")),
            bool(attrs.get("goal_text")),
            bool(attrs.get("intent")),
            bool(attrs.get("answer")),
        ]
        if sum(provided) != 1:
            raise serializers.ValidationError(
                "Provide exactly one of: goal_key, goal_text, intent, answer."
            )
        if (attrs.get("goal_key") or attrs.get("goal_text")) and not attrs.get(
            "source_channel"
        ):
            raise serializers.ValidationError(
                {"source_channel": "Required when selecting a goal."}
            )
        return attrs


def _emit_goal_selected(*, client, goal: ClientGoal) -> None:
    """Событие воронки goal_selected. Эмиссия серверная: клиентских
    client_event_id у нас нет — генерируем; повторная запись той же цели
    = новая строка (смена цели и есть событие)."""
    AnalyticsEvent.objects.create(
        event_name=event_catalogue.GOAL_SELECTED,
        payload={
            "goal_key": goal.goal_key,
            "has_text": bool(goal.goal_text),
            "source_channel": goal.source_channel,
        },
        actor=client,
        app_type=AnalyticsEvent.AppType.CLIENT,
        client_event_id=uuid.uuid4(),
    )


def _create_goal(
    *,
    client,
    goal_key: str | None,
    goal_text: str | None,
    source_channel: str,
) -> ClientGoal:
    """Записать новую активную цель, закрыв прежнюю.

    Вынесено из ``GoalSelectView.post``, потому что теперь сюда ведут два
    пути: прямой выбор (чип / свободный ввод) и завершение анкеты.

    Салона здесь нет (DRF-1472). DRF-1455 проставлял его на цель, чтобы
    судить о ней по каталогу одного салона; владелец это отменил,
    разобравшись, что цели спрашивает только клиентский бот-витрина, у
    которого салона нет нарочно. Записывать салон «на всякий случай»
    было бы хуже, чем не записывать: колонка, ни на что не влияющая,
    рано или поздно начинает влиять.
    """
    with transaction.atomic():
        ClientGoal.objects.filter(client=client, is_active=True).update(
            is_active=False
        )
        return ClientGoal.objects.create(
            client=client,
            goal_key=goal_key or None,
            goal_text=(goal_text or "").strip() or None,
            source_channel=source_channel,
        )


def _close_open_run(client, *, goal: ClientGoal | None) -> None:
    """Закрыть незакрытый проход анкеты.

    Вызывается на КАЖДОМ прямом выборе цели — и это и есть «анкета не
    ворота» в исполнении (условие C-2 поправки A-1). Человек, назвавший
    услугу на втором вопросе, уходит к подбору; оставить проход открытым
    значило бы на следующем же запросе снова показать ему вопрос, то
    есть уронить обратно в анкету, что решение владельца запрещает
    прямым текстом.
    """
    run = open_anketa_run(client)
    if run is None:
        return
    run.completed_at = timezone.now()
    run.goal = goal
    run.save(update_fields=["completed_at", "goal"])


class DecisionContextView(APIView):
    """GET /api/v1/internal/me/decision-context/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            200: OpenApiResponse(description="Decision context document"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def get(self, request: Request) -> Response:
        return success_response(build_decision_context(request.user))


class GoalSelectView(APIView):
    """POST /api/v1/internal/me/goals/select/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        request=GoalSelectSerializer,
        responses={
            200: OpenApiResponse(description="Updated decision context document"),
            400: OpenApiResponse(description="Validation error"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = GoalSelectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("intent") == INTENT_NEED_GUIDANCE:
            # «Не понимаю, чего хочу» — состояние ведения, не выход и не
            # цель: ClientGoal не создаём, событие не эмитим, возвращаем
            # документ с первым ведущим вопросом (Ответ 3).
            logger.info("goals.need_guidance user_id=%s", request.user.id)
            return success_response(
                build_decision_context(request.user, guidance=True)
            )

        if data.get("intent") == INTENT_START_ANKETA:
            # Повторный проход (DRF-1225 / C-4). Активную цель НЕ
            # трогаем: она остаётся в силе до тех пор, пока новый проход
            # не сформирует новую. Бросить анкету на середине не должно
            # означать остаться без цели.
            return success_response(self._start_anketa(request.user))

        if data.get("answer") is not None:
            return self._answer_anketa(
                request.user,
                answer=data["answer"],
                source_channel=data["source_channel"],
            )

        goal = _create_goal(
            client=request.user,
            goal_key=data.get("goal_key"),
            goal_text=data.get("goal_text"),
            source_channel=data["source_channel"],
        )
        # Прямой выбор закрывает открытый проход: см. _close_open_run.
        _close_open_run(request.user, goal=goal)
        _emit_goal_selected(client=request.user, goal=goal)

        logger.info(
            "goals.selected user_id=%s goal_key=%r has_text=%s channel=%s",
            request.user.id, goal.goal_key, bool(goal.goal_text),
            goal.source_channel,
        )
        return success_response(build_decision_context(request.user))

    # -- анкета (DRF-1451) -------------------------------------------------

    @staticmethod
    def _start_anketa(client) -> dict:
        """Начать новый проход. Уже открытый переиспользуется.

        ``get_or_create``, а не проверка-и-создание: ``GoalAnketaRun``
        держит partial-UniqueConstraint «один открытый проход на
        клиента», и два одновременных запроса (бот и мини-апп — два
        независимых вызывающих на одном клиенте) прошли бы проверку оба,
        а INSERT второго упал бы в 500. ``get_or_create`` ловит
        IntegrityError и перечитывает.
        """
        run, created = GoalAnketaRun.objects.get_or_create(
            client=client, completed_at=None
        )
        if created:
            logger.info("goals.anketa_started user_id=%s run_id=%s", client.id, run.id)
        return build_decision_context(client)

    @transaction.atomic
    def _answer_anketa(
        self,
        client,
        *,
        answer: dict,
        source_channel: str,
    ) -> Response:
        """Записать ответ на шаг; последний шаг формирует цель.

        Шаг, который сервер ждёт, вычисляется здесь заново — эхо клиента
        только сверяется с ним. Поэтому пропустить вопрос, ответить не по
        порядку или переписать уже закрытый проход из клиента нельзя:
        последовательность остаётся серверной (условие C-1).

        # Отклонённый ответ не создаёт прохода

        Порядок здесь не косметический. Раньше строка прохода писалась
        ПЕРВОЙ, до сверки эха, — и каждый 409 (протухший документ на
        экране, повтор после таймаута) оставлял человеку открытый проход
        навсегда. Дальше ``build_decision_context`` видел незакрытый
        проход и на КАЖДОМ запросе возвращал вопрос анкеты: человек с
        целью получал анкету при каждом открытии приложения. То есть
        ровно то, что решение владельца запрещает.

        Теперь проход создаётся последним из проверок, и ни один путь
        отказа до него не доходит. ``@transaction.atomic`` закрывает
        второй край того же: создание цели, запись ответа и закрытие
        прохода либо происходят вместе, либо не происходят вовсе —
        иначе цель существовала бы при открытом проходе, и человека
        снова тянуло бы в вопросы.
        """
        run = open_anketa_run(client)

        # Сверки — ДО любой записи.
        expected = next_anketa_step(run)
        if answer["step"] != expected.key:
            return error_response(
                ErrorCode.ANKETA_STEP_MISMATCH,
                "Answer does not match the expected step.",
                details={"expected_step": expected.key},
                status_code=409,
            )

        option_key = answer.get("option_key")
        text = (answer.get("text") or "").strip() or None
        if option_key:
            # Без `and expected.options`: на салоне без активных
            # GoalOption список финального шага пуст, и прежний вид
            # проверки пропускал ЛЮБОЙ слаг прямо в ClientGoal.goal_key.
            allowed = {key for key, _ in expected.options}
            if option_key not in allowed:
                raise serializers.ValidationError(
                    {"answer": {"option_key": "Unknown option for this step."}}
                )
        if text and not expected.allow_free_text:
            raise serializers.ValidationError(
                {"answer": {"text": "This step does not accept free text."}}
            )

        # Проверки пройдены — только теперь можно писать.
        if run is None:
            run, _ = GoalAnketaRun.objects.get_or_create(client=client, completed_at=None)

        if expected.key != anketa.FINAL_STEP_KEY:
            GoalAnketaAnswer.objects.update_or_create(
                run=run,
                step_key=expected.key,
                defaults={"option_key": option_key, "answer_text": text},
            )
            logger.info(
                "goals.anketa_answered user_id=%s run_id=%s step=%s",
                client.id, run.id, expected.key,
            )
            return success_response(build_decision_context(client))

        # Финальный шаг. Завершение анкеты И ЕСТЬ выбор цели — отдельной
        # кнопки «готово» нет, потому что нечего было бы подтверждать.
        goal = _create_goal(
            client=client,
            goal_key=option_key,
            goal_text=text,
            source_channel=source_channel,
        )
        GoalAnketaAnswer.objects.update_or_create(
            run=run,
            step_key=expected.key,
            defaults={"option_key": option_key, "answer_text": text},
        )
        _close_open_run(client, goal=goal)
        _emit_goal_selected(client=client, goal=goal)
        logger.info(
            "goals.anketa_completed user_id=%s run_id=%s goal_key=%r",
            client.id, run.id, goal.goal_key,
        )
        return success_response(build_decision_context(client))
