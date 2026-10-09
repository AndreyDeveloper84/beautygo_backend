"""Вторая линия гейта у ручек движка Плана: заявка на удаление и отзыв согласия (решение владельца 09.10.2026).

При активной заявке на удаление: «сохранённый план можно просматривать;
нельзя собирать новый план; нельзя редактировать или пересобирать план;
нельзя подбирать услугу, переходить к записи или создавать запись; нельзя
запускать новую обработку или пересчёт». «После фактического удаления план
больше не показывается.» «Каталог остаётся второй fail-closed линией.»

Узлы держат:

* каждая пишущая и обрабатывающая ручка отвечает человеку с открытой заявкой
  отказом 423 с номером заявки и НИЧЕГО не меняет;
* чтение своего плана и подписи шагов остаются открытыми, и чтение ничего
  не пишет (использование плана не продлевает хранение);
* запись от шага плана закрыта; обычная запись без шага сюда не относится;
* без заявки всё работает как раньше (четвёртый случай владельца);
* гейт стоит на КАЖДОМ пишущем методе файла ручек: новая ручка без гейта и
  без записи в перечне исключений краснит узел.

Четыре случая владельца на каталожной линии:

* активная заявка на удаление — блок (узлы ниже);
* согласие отозвано — блок: каталог знает отзыв, как только бот его
  доставил (узлы ниже);
* действующее согласие без заявки — обычная работа (узлы ниже);
* **согласия не было никогда — этим шагом НЕ закрыто.** «Не было» и «бот
  не сообщал» каталогу неразличимы: строки состояния нет в обоих случаях.
  Узел ниже фиксирует сегодняшнее поведение как названный пробел; закрывает
  его следующий шаг — утверждение основания в теле запроса.
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from users.models import DeletionRequest
from wellness import plan_engine_api
from wellness.models import Plan
from wellness.tests.test_plan_compose_2871 import OWNER, _api, _body
from wellness.tests.test_plan_engine_2857 import _command, _save

pytestmark = pytest.mark.django_db

PLAN_URL = "/api/v1/internal/me/plan/"
URLS = {
    "save": PLAN_URL,
    "state": "/api/v1/internal/me/plan/state/",
    "decision": "/api/v1/internal/me/plan/decision/",
    "resolution": "/api/v1/internal/me/plan/steps/resolution/",
    "booking": "/api/v1/internal/me/plan/steps/booking/",
    "restriction": "/api/v1/internal/me/plan/restrictions/",
    "lift": "/api/v1/internal/me/plan/restrictions/lift/",
    "replace": "/api/v1/internal/me/plan/replace/",
}
LABELS_URL = "/api/v1/internal/me/plan/capability-labels/"

#: Пишущие методы БЕЗ гейта — каждый с причиной. Сюда нельзя добавить ручку
#: молча: узел ниже сверяет перечень с файлом.
UNGATED_ON_PURPOSE = {
    "PlanCapabilityLabelsView.post": (
        "чтение знания каталога, нужное и для показа сохранённого плана; о человеке ничего не читает и не пишет"
    ),
}


@pytest.fixture(autouse=True)
def _token_and_flag(settings):
    from wellness.tests.test_plan_compose_2871 import VALID_TOKEN

    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_ENGINE_ENABLED = True


@pytest.fixture
def owner(db):
    from wellness.tests.test_plan_compose_2871 import _user

    return _user(OWNER, "+79995028799", is_proxy=True)


@pytest.fixture
def goal(owner):
    from goals.models import ClientGoal
    from services.models import GoalOption

    GoalOption.objects.create(key="gate-goal", label="Цель гейта")
    return ClientGoal.objects.create(client=owner, goal_key="gate-goal", source_channel="bot")


def _request_deletion(user, status=DeletionRequest.Status.REQUESTED) -> DeletionRequest:
    return DeletionRequest.objects.create(
        user=user, initiator="bot", status=status, deadline_at=timezone.now() + timedelta(days=30),
    )


def _bodies(goal, saved=None) -> dict:
    """Тело на каждую ручку. Гейт стоит раньше разбора тела; у смены состояния — настоящая форма: пауза."""
    return {
        "save": _command(goal),
        "state": {"plan_id": str(saved.pk) if saved is not None else None, "state": Plan.Status.PAUSED},
        "decision": _body(), "resolution": {}, "booking": {}, "restriction": {}, "lift": {}, "replace": {},
    }


def _writes(queries) -> list[str]:
    return [q["sql"] for q in queries if q["sql"].lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}]


# ─── заявка на удаление закрывает запись и обработку ─────────────────────────


@pytest.mark.parametrize("endpoint", sorted(URLS))
def test_an_open_deletion_request_closes_every_writing_endpoint(owner, goal, endpoint) -> None:
    saved = _save(goal)
    request = _request_deletion(owner)
    before = (Plan.objects.count(), Plan.objects.filter(pk=saved.pk).values().get())

    with CaptureQueriesContext(connection) as queries:
        response = _api().post(URLS[endpoint], _bodies(goal, saved)[endpoint], format="json")

    assert response.status_code == 423, (endpoint, response.content[:300])
    error = response.json()["error"]
    assert error["code"] == "DELETION_IN_PROGRESS"
    assert error["details"]["reason"] == "deletion_requested"
    assert error["details"]["request_id"] == str(request.pk)
    assert (Plan.objects.count(), Plan.objects.filter(pk=saved.pk).values().get()) == before
    assert _writes(queries.captured_queries) == []


def test_a_new_plan_is_not_saved_under_a_deletion_request(owner, goal) -> None:
    _request_deletion(owner)

    response = _api().post(PLAN_URL, _command(goal), format="json")

    assert response.status_code == 423
    assert Plan.objects.count() == 0


@pytest.mark.parametrize("status", [
    DeletionRequest.Status.REQUESTED,
    *[s for s in DeletionRequest.OPEN_STATUSES if s != DeletionRequest.Status.REQUESTED],
])
def test_every_open_status_of_the_request_blocks(owner, goal, status) -> None:
    _request_deletion(owner, status=status)

    assert _api().post(URLS["decision"], _body(), format="json").status_code == 423


# ─── архив своего плана открыт ────────────────────────────────────────────────


@pytest.mark.parametrize("blocker", ["deletion_request", "withdrawn_consent"])
def test_ones_own_plan_can_be_archived_under_the_gate(owner, goal, blocker) -> None:
    """В списке запретов владельца архива нет: это распоряжение своим, а не новая обработка."""
    saved = _save(goal)
    if blocker == "deletion_request":
        _request_deletion(owner)
    else:
        _consent(owner, granted=False)

    paused = _api().post(URLS["state"], {"plan_id": str(saved.pk), "state": Plan.Status.PAUSED}, format="json")
    archived = _api().post(URLS["state"], {"plan_id": str(saved.pk), "state": Plan.Status.ARCHIVED}, format="json")

    assert paused.status_code in (422, 423), paused.content[:300]  # прочие смены состояния закрыты
    assert archived.status_code == 200, archived.content[:300]
    assert Plan.objects.get(pk=saved.pk).status == Plan.Status.ARCHIVED


def test_control_the_plan_is_paused_without_any_blocker(owner, goal) -> None:
    """Положительная пара к узлу выше: та же пауза без заявки и отзыва проходит."""
    saved = _save(goal)

    response = _api().post(URLS["state"], {"plan_id": str(saved.pk), "state": Plan.Status.PAUSED}, format="json")

    assert response.status_code == 200, response.content[:300]
    assert Plan.objects.get(pk=saved.pk).status == Plan.Status.PAUSED


# ─── отзыв согласия на хранение ───────────────────────────────────────────────


def _consent(user, *, granted: bool):
    from users.consent_events import PERSONAL_DATA
    from users.models import ConsentState

    return ConsentState.objects.create(
        user=user, consent_type=PERSONAL_DATA, granted=granted, granted_at=timezone.now(),
        event_id=f"gate-{user.pk}-{granted}",
    )


@pytest.mark.parametrize("endpoint", sorted(URLS))
def test_a_withdrawn_storage_consent_closes_every_writing_endpoint(owner, goal, endpoint) -> None:
    saved = _save(goal)
    _consent(owner, granted=False)
    before = (Plan.objects.count(), Plan.objects.filter(pk=saved.pk).values().get())

    with CaptureQueriesContext(connection) as queries:
        response = _api().post(URLS[endpoint], _bodies(goal, saved)[endpoint], format="json")

    assert response.status_code == 422, (endpoint, response.content[:300])
    error = response.json()["error"]
    assert error["code"] == "CONSENT_REQUIRED"
    assert error["details"] == {"consent_type": "personal_data", "reason": "withdrawn"}
    assert (Plan.objects.count(), Plan.objects.filter(pk=saved.pk).values().get()) == before
    assert _writes(queries.captured_queries) == []


def _attested(granted_at, **over) -> dict:
    block = {"type": "personal_data", "document_version": "2026-10", "granted_at": granted_at.isoformat()}
    block.update(over)
    return {"consent": block}


def test_a_consent_given_again_after_the_withdrawal_reopens_the_plan(owner, goal) -> None:
    """Бот об отзыве сообщает, а о новом согласии — нет: без сравнения времени человек был бы закрыт навсегда."""
    withdrawal = _consent(owner, granted=False)
    again = withdrawal.granted_at + timedelta(minutes=5)

    composed = _api().post(URLS["decision"], {**_body(), **_attested(again)}, format="json")
    saved = _api().post(PLAN_URL, {**_command(goal), **_attested(again)}, format="json")

    assert composed.status_code == 200, composed.content[:300]
    assert saved.status_code == 201, saved.content[:300]


@pytest.mark.parametrize("case", [
    "older_than_the_withdrawal", "same_instant", "no_timezone", "not_a_date", "wrong_kind", "empty_version",
    "not_an_object",
])
def test_a_stale_or_unusable_attestation_does_not_beat_the_withdrawal(owner, goal, case) -> None:
    """«Бот проверил по устаревшему состоянию» — отказ; негодное утверждение ничем не лучше отсутствующего."""
    withdrawal = _consent(owner, granted=False)
    later = withdrawal.granted_at + timedelta(minutes=5)
    body = {
        "older_than_the_withdrawal": _attested(withdrawal.granted_at - timedelta(days=1)),
        "same_instant": _attested(withdrawal.granted_at),
        "no_timezone": {"consent": {
            **_attested(later)["consent"], "granted_at": later.replace(tzinfo=None).isoformat(),
        }},
        "not_a_date": {"consent": {**_attested(later)["consent"], "granted_at": "вчера"}},
        "wrong_kind": _attested(later, type="health"),
        "empty_version": _attested(later, document_version="  "),
        "not_an_object": {"consent": "personal_data"},
    }[case]

    response = _api().post(URLS["decision"], {**_body(), **body}, format="json")

    assert response.status_code == 422, (case, response.content[:300])
    assert response.json()["error"]["details"]["reason"] == "withdrawn"


def test_a_consent_record_without_a_text_version_is_still_a_consent(owner, goal) -> None:
    """У старых записей согласия версии текста нет; бот шлёт явную метку — её значение каталог не толкует."""
    withdrawal = _consent(owner, granted=False)

    response = _api().post(URLS["decision"], {
        **_body(), **_attested(withdrawal.granted_at + timedelta(minutes=5), document_version="unversioned"),
    }, format="json")

    assert response.status_code == 200, response.content[:300]


def test_the_latest_withdrawal_of_the_person_is_the_one_compared(owner, goal) -> None:
    """Согласие дано между двумя отзывами — действует последний отзыв."""
    from users.consent_events import PERSONAL_DATA
    from users.models import ConsentState

    first = timezone.now() - timedelta(days=3)
    ConsentState.objects.create(
        user=owner, consent_type=PERSONAL_DATA, granted=False, granted_at=timezone.now(), event_id="gate-latest",
    )

    response = _api().post(URLS["decision"], {**_body(), **_attested(first + timedelta(days=1))}, format="json")

    assert response.status_code == 422


def test_a_granted_storage_consent_lets_the_plan_work(owner, goal) -> None:
    _consent(owner, granted=True)

    assert _api().post(PLAN_URL, _command(goal), format="json").status_code == 201
    assert _api().post(URLS["decision"], _body(), format="json").status_code == 200


def test_the_plan_can_still_be_read_after_the_consent_is_withdrawn(owner, goal) -> None:
    """«Сохранённый план не уничтожается самим отзывом согласия» и остаётся виден."""
    saved = _save(goal)
    _consent(owner, granted=False)

    response = _api().get(PLAN_URL)

    assert response.status_code == 200
    assert response.json()["data"]["plan"]["plan_id"] == str(saved.pk)
    assert _api().post(LABELS_URL, {"keys": ["any_key"]}, format="json").status_code == 200


def test_a_deletion_request_answers_before_the_consent(owner, goal) -> None:
    """У заявки есть номер и срок — человек на любом экране видит один отказ."""
    _consent(owner, granted=False)
    request = _request_deletion(owner)

    response = _api().post(URLS["decision"], _body(), format="json")

    assert response.status_code == 423
    assert response.json()["error"]["details"]["request_id"] == str(request.pk)


def test_a_withdrawal_of_another_consent_kind_does_not_close_the_plan(owner, goal) -> None:
    from users.consent_events import FOOD_DIARY_PROCESSING
    from users.models import ConsentState

    ConsentState.objects.create(
        user=owner, consent_type=FOOD_DIARY_PROCESSING, granted=False, granted_at=timezone.now(), event_id="gate-other",
    )

    assert _api().post(URLS["decision"], _body(), format="json").status_code == 200


def test_the_catalog_now_keeps_the_storage_consent_the_bot_delivers(owner) -> None:
    """До правки событие этого вида каталог принимал и выбрасывал — отзыв ему был неизвестен."""
    from users.consent_events import PERSONAL_DATA, ConsentEvent, apply_consent_event
    from users.models import ConsentState

    applied = apply_consent_event(owner, ConsentEvent(
        event_id="gate-delivery-1", consent_type=PERSONAL_DATA, granted=False, granted_at=timezone.now(),
    ))

    assert applied.outcome == "applied" and applied.erased == ()
    assert ConsentState.objects.get(user=owner, consent_type=PERSONAL_DATA).granted is False


def test_known_gap_a_person_the_bot_never_reported_is_not_blocked_yet(owner, goal) -> None:
    """НАЗВАННЫЙ ПРОБЕЛ, не желаемое поведение: случай владельца «без согласия — блок» здесь не выполнен.

    Строки состояния нет и у того, кто не соглашался, и у того, о ком бот не
    сообщал. Закрывается следующим шагом — утверждением основания в теле
    запроса; когда он появится, этот узел обязан стать «отказ».
    """
    from users.models import ConsentState

    assert not ConsentState.objects.filter(user=owner).exists()

    assert _api().post(URLS["decision"], _body(), format="json").status_code == 200


def test_an_unknown_top_level_field_does_not_break_saving_or_composing(owner, goal) -> None:
    """Вызывающие начнут слать утверждение основания раньше, чем каталог начнёт его требовать."""
    attestation = _attested(timezone.now())

    saved = _api().post(PLAN_URL, {**_command(goal), **attestation}, format="json")
    composed = _api().post(URLS["decision"], {**_body(), **attestation}, format="json")

    assert saved.status_code == 201, saved.content[:300]
    assert composed.status_code == 200, composed.content[:300]


# ─── чтение остаётся и ничего не пишет ───────────────────────────────────────


def test_the_saved_plan_can_still_be_read_and_reading_writes_nothing(owner, goal) -> None:
    saved = _save(goal)
    _request_deletion(owner)

    with CaptureQueriesContext(connection) as queries:
        response = _api().get(PLAN_URL)

    assert response.status_code == 200, response.content[:300]
    assert response.json()["data"]["plan"]["plan_id"] == str(saved.pk)
    assert _writes(queries.captured_queries) == []


def test_reading_writes_nothing_without_a_request_either(owner, goal) -> None:
    """Использование плана не продлевает срок его хранения — чтение не пишет никогда."""
    _save(goal)

    with CaptureQueriesContext(connection) as queries:
        assert _api().get(PLAN_URL).status_code == 200

    assert _writes(queries.captured_queries) == []


def test_step_labels_stay_readable_under_a_deletion_request(owner, goal) -> None:
    """Иначе человек видел бы свой сохранённый план списком ключей."""
    _request_deletion(owner)

    response = _api().post(LABELS_URL, {"keys": ["any_key"]}, format="json")

    assert response.status_code == 200, response.content[:300]


# ─── без заявки — обычная работа ─────────────────────────────────────────────


def test_without_a_request_the_plan_is_saved_and_composed_as_before(owner, goal) -> None:
    saved = _api().post(PLAN_URL, _command(goal), format="json")
    composed = _api().post(URLS["decision"], _body(), format="json")

    assert saved.status_code == 201, saved.content[:300]
    assert composed.status_code == 200, composed.content[:300]


def test_a_finished_or_cancelled_request_does_not_block(owner, goal) -> None:
    closed = [s for s in DeletionRequest.Status.values if s not in DeletionRequest.OPEN_STATUSES]
    assert closed, "положительная пара: закрытые состояния заявки существуют"
    for status in closed:
        DeletionRequest.objects.create(
            user=owner, initiator="bot", status=status, deadline_at=timezone.now(), completed_at=timezone.now(),
        )

    assert _api().post(URLS["decision"], _body(), format="json").status_code == 200


def test_someone_elses_request_does_not_block_me(owner, goal) -> None:
    from wellness.tests.test_plan_compose_2871 import _user

    _request_deletion(_user("bot:plan-gate-stranger", "+79995028798", is_proxy=True))

    assert _api().post(URLS["decision"], _body(), format="json").status_code == 200


# ─── перепись: гейт на каждом пишущем методе ─────────────────────────────────


def test_every_writing_method_of_the_plan_endpoints_is_gated_or_named() -> None:
    """Новая пишущая ручка без гейта не проходит молча: её либо закрывают, либо называют причину."""
    writing = ("post", "put", "patch", "delete")
    ungated = set()
    gated = set()
    for name, view in inspect.getmembers(plan_engine_api, inspect.isclass):
        if view.__module__ != plan_engine_api.__name__ or not name.endswith("View"):
            continue
        for method in writing:
            handler = view.__dict__.get(method)
            if handler is None:
                continue
            (gated if getattr(handler, "plan_gate", False) else ungated).add(f"{name}.{method}")

    assert len(gated) >= 8, gated  # положительная пара: перепись что-то нашла
    assert ungated == set(UNGATED_ON_PURPOSE), (
        "пишущий метод без гейта и без названной причины: "
        f"{sorted(ungated - set(UNGATED_ON_PURPOSE))}; лишнее в перечне: {sorted(set(UNGATED_ON_PURPOSE) - ungated)}"
    )


def test_reading_methods_are_not_gated() -> None:
    for name, view in inspect.getmembers(plan_engine_api, inspect.isclass):
        handler = getattr(view, "__dict__", {}).get("get")
        if handler is not None and view.__module__ == plan_engine_api.__name__:
            assert not getattr(handler, "plan_gate", False), f"{name}.get под гейтом — чтение своего плана закрыто"


# ─── сквозной провод: снимки бота, а не представление о них ──────────────────
#
# Правило «отзыв побеждает, только если он позже утверждения» держится на том,
# что бот кладёт в событие отзыва МОМЕНТ ОТЗЫВА. Ниже — тела, снятые
# исполнением на боте 09.10.2026 (ветка поверх dev 5539860e, один человек,
# один прогон: согласие → отзыв → сразу согласие снова): настоящие
# ``withdraw`` → публикация события → подписчик каталога → тело на входе
# отправки. Подменён только сам HTTP-вызов. Положены дословно; идентификатор
# события заменён (в снимке он — uuid конверта).
#
# Чего снимок не покрывает: диспетчер шины и сам HTTP бота исполнены не были.

BOT_WITHDRAWAL_EVENT = {
    "event_id": "0f6f1c1e-2967-4a6b-9d1e-5a3d7c9e2967",
    "consent_type": "personal_data",
    "granted": False,
    "granted_at": "2026-10-09T13:37:55.402340+00:00",
    "granted_via": "chat",
}
#: Утверждение после нового согласия того же человека (глобальный путь).
BOT_ATTESTATION_AFTER_RECONSENT = {
    "type": "personal_data", "document_version": "unversioned", "granted_at": "2026-10-09T13:37:55.544001+00:00",
}
#: Утверждение по прежней, отозванной записи — снято до отзыва.
BOT_ATTESTATION_BEFORE_WITHDRAWAL = {
    "type": "personal_data", "document_version": "unversioned", "granted_at": "2026-10-09T13:37:55.378857+00:00",
}
CONSENT_EVENTS_URL = "/api/v1/internal/me/consent-events/"


def _deliver_withdrawal() -> None:
    """Событие отзыва приходит в каталог тем же путём, что с бота: настоящей ручкой, телом как есть."""
    response = _api().post(CONSENT_EVENTS_URL, BOT_WITHDRAWAL_EVENT, format="json")
    assert response.status_code == 200, response.content[:300]
    assert response.json()["data"]["outcome"] == "applied", response.content[:300]


def test_wire_withdrew_then_consented_again_and_the_catalog_does_not_block(owner, goal) -> None:
    """Четвёртый случай владельца на настоящих метках времени: действующее согласие — обычная работа."""
    _deliver_withdrawal()
    attestation = {"consent": BOT_ATTESTATION_AFTER_RECONSENT}

    composed = _api().post(URLS["decision"], {**_body(), **attestation}, format="json")
    saved = _api().post(PLAN_URL, {**_command(goal), **attestation}, format="json")

    assert composed.status_code == 200, composed.content[:300]
    assert saved.status_code == 201, saved.content[:300]


def test_wire_the_attestation_of_the_withdrawn_record_is_refused(owner, goal) -> None:
    """Контроль: тот же человек, утверждение по записи, которую он уже отозвал, — отказ."""
    _deliver_withdrawal()

    response = _api().post(
        URLS["decision"], {**_body(), "consent": BOT_ATTESTATION_BEFORE_WITHDRAWAL}, format="json",
    )

    assert response.status_code == 422, response.content[:300]
    assert response.json()["error"]["details"] == {"consent_type": "personal_data", "reason": "withdrawn"}


def test_wire_the_withdrawal_alone_blocks(owner, goal) -> None:
    """Контроль: событие отзыва действительно дошло и закрыло план — иначе два узла выше ничего бы не значили."""
    assert _api().post(URLS["decision"], _body(), format="json").status_code == 200

    _deliver_withdrawal()

    assert _api().post(URLS["decision"], _body(), format="json").status_code == 422


def test_wire_the_snapshots_are_in_the_order_the_rule_relies_on() -> None:
    """Сами снимки: прежнее согласие < отзыв < новое согласие — как моменты времени, а не строки."""
    from datetime import datetime

    before, withdrawn, after = (
        datetime.fromisoformat(value) for value in (
            BOT_ATTESTATION_BEFORE_WITHDRAWAL["granted_at"], BOT_WITHDRAWAL_EVENT["granted_at"],
            BOT_ATTESTATION_AFTER_RECONSENT["granted_at"],
        )
    )

    assert before < withdrawn < after
    assert all(moment.tzinfo is not None for moment in (before, withdrawn, after))


# ─── выключенный движок отвечает «движок выключен», а не отказом гейта ───────


@pytest.mark.parametrize("blocker", ["deletion_request", "withdrawn_consent"])
def test_a_disabled_engine_answers_disabled_not_the_gate(settings, owner, goal, blocker) -> None:
    """По 404 вызывающий решает «движка нет — работаю по-старому»; отказ гейта читался бы как «нет согласия»."""
    if blocker == "deletion_request":
        _request_deletion(owner)
    else:
        _consent(owner, granted=False)
    settings.PLAN_ENGINE_ENABLED = False

    for endpoint in ("decision", "save"):
        response = _api().post(URLS[endpoint], _bodies(goal)[endpoint], format="json")
        assert response.status_code == 404, (endpoint, response.content[:300])
        assert response.json()["error"]["code"] == "PLAN_ENGINE_DISABLED", endpoint
    assert Plan.objects.count() == 0


def test_control_the_gate_answers_once_the_engine_is_on(settings, owner, goal) -> None:
    """Положительная пара: тот же человек, движок включён — отвечает гейт."""
    _request_deletion(owner)
    settings.PLAN_ENGINE_ENABLED = True

    assert _api().post(URLS["decision"], _body(), format="json").status_code == 423
