"""Потерянная запись обязана быть видна машине, а не только глазу.

Owner F6, 16.09.2026, дословно: обычное чтение человеком **своих** данных не
останавливается из-за временного отказа записи аудита — операция продолжается,
``privacy_audit.record_lost`` фиксируется, есть counter/metric, есть
correlation id, есть alert при повторении или пороговом количестве.

И там же — **условие полноты**, ради которого существует этот файл::

    Если ERROR никто не читает и нет metric/alert — вариант (б) НЕ считается
    полностью реализованным.

Поэтому тесты ниже проверяют не «строка написалась». Строка написалась и до
F6. Проверяется то, чего не было: **счётчик, который можно спросить**,
**correlation id, по которому потерю связывают с запросом**, и **сигнал,
который будит человека** — причём последний обязан оставаться необязательным:
без Sentry первые два не должны исчезнуть.

Разделение владельца при этом не трогается: операции, которые раскрывают или
разрушают, по-прежнему отказывают (503) и в потери не попадают — это отдельный
узел ниже, а не подразумеваемое следствие.
"""
from __future__ import annotations

import logging
import sys
import uuid

import pytest

from privacy_audit import observability, policy, services
from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.services import AuditUnavailable

_Op = PersonalDataAccessLog.Operation
_Result = PersonalDataAccessLog.Result
_Category = PersonalDataAccessLog.ObjectCategory


@pytest.fixture(autouse=True)
def _fresh_counter():
    """Счётчик живёт в процессе, значит протекает между тестами.

    Сброс здесь, а не в коде: в бою обнулять счётчик потерь нечем и незачем.
    """
    observability.LOST_RECORDS.reset()
    yield
    observability.LOST_RECORDS.reset()


@pytest.fixture
def broken_journal(monkeypatch):
    """Прямая запись в журнал падает — как при недоступной таблице.

    Подменяется ``services.record_access``, а не сам INSERT: маршрутизация
    §107 (остановить или пропустить) должна отработать по-настоящему.
    """
    def _boom(**_kwargs):
        raise AuditUnavailable("simulated journal outage")

    monkeypatch.setattr(services, "record_access", _boom)


def _lose(
    *,
    operation: str = _Op.READ_CONTEXT,
    result: str = _Result.ALLOWED,
    subject=None,
    request_id: str = "req-f6-0001",
) -> str:
    """Один проход через боевую развилку: вернуть ``"lost"`` или бросить."""
    return services.record_or_lose(
        caller_purpose=PersonalDataAccessLog.CallerPurpose.INTERNAL,
        actor=None,
        object_id=subject or uuid.uuid4(),
        operation=operation,
        object_category=_Category.PERSONAL_CONTEXT,
        result=result,
        actor_named=True,
        request_id=request_id,
    )


class TestTheLossIsCounted:
    """«Счётчик есть» — значит его можно СПРОСИТЬ, а не «в консоли что-то было».

    Пустая консоль и перезапущенный контейнер выглядят одинаково; величина,
    которую можно прочитать из процесса, их различает. Тот же довод уже
    записан в модели журнала про ``actor_named`` — здесь он применён к потере.
    """

    def test_a_lost_record_moves_a_counter_a_machine_can_read(self, broken_journal):
        assert observability.LOST_RECORDS.total == 0

        assert _lose() == "lost"

        assert observability.LOST_RECORDS.total == 1

    def test_the_counter_names_the_operation_not_only_a_total(self, broken_journal):
        _lose(operation=_Op.READ_CONTEXT)
        _lose(operation=_Op.READ_CONTEXT)
        _lose(operation=_Op.WRITE_CONTEXT)

        assert observability.LOST_RECORDS.total == 3
        assert observability.LOST_RECORDS.by_operation() == {
            _Op.READ_CONTEXT: 2,
            _Op.WRITE_CONTEXT: 1,
        }

    def test_a_written_record_does_not_move_the_counter(self, monkeypatch):
        """Положительный контроль: счётчик, который растёт всегда, не счётчик.

        Без этого узла «3» ниже доказывала бы только то, что код выполнялся.
        """
        monkeypatch.setattr(services, "record_access", lambda **_kw: object())

        assert _lose() == "written"

        assert observability.LOST_RECORDS.total == 0

    def test_a_refused_consequential_operation_is_not_counted_as_lost(
        self, broken_journal,
    ):
        """Граница владельца, проверенная с той стороны, где её легко стереть.

        Экспорт при недоступном журнале НЕ происходит — значит и «потери»
        нет: ничего не раскрыли. Счётчик потерь, который считал бы ещё и
        отказы, смешал бы два разных события под одним именем, и порог
        срабатывал бы от безопасных отказов.
        """
        assert policy.stops_when_unauditable(_Op.EXPORT)

        with pytest.raises(AuditUnavailable):
            _lose(operation=_Op.EXPORT)

        assert observability.LOST_RECORDS.total == 0
        assert observability.LOST_RECORDS.by_operation() == {}


class TestTheLossCarriesACorrelationId:
    """Потеря без correlation id — событие, которое не с чем сопоставить.

    Строка ERROR отвечает «что-то потеряли»; correlation id отвечает «вот
    этот запрос», и только он соединяет потерю с остальным следом запроса.
    """

    def test_the_error_line_carries_the_correlation_id(self, broken_journal, caplog):
        with caplog.at_level(logging.ERROR, logger="privacy_audit"):
            _lose(request_id="req-f6-abc123")

        lost = [r for r in caplog.records if "privacy_audit.record_lost" in r.getMessage()]
        assert len(lost) == 1, [r.getMessage() for r in caplog.records]
        assert "request_id=req-f6-abc123" in lost[0].getMessage()

    def test_the_error_line_carries_the_running_count(self, broken_journal, caplog):
        """Число рядом с событием: по одной строке видно, первая это потеря
        или двухсотая. Иначе «сколько» приходится собирать из грепа."""
        with caplog.at_level(logging.ERROR, logger="privacy_audit"):
            _lose()
            _lose()

        lost = [r.getMessage() for r in caplog.records
                if "privacy_audit.record_lost" in r.getMessage()]
        assert len(lost) == 2, lost
        assert "lost_total=1" in lost[0]
        assert "lost_total=2" in lost[1]

    def test_an_absent_correlation_id_is_named_not_invented(self, broken_journal, caplog):
        """«Никто не назвал» и «вот значение» обязаны остаться различимы.

        Подставленный сюда правдоподобный id склеил бы потерю с чужим
        запросом — в точности та ошибка, ради которой ``basis`` в журнале
        оставлен пустым.
        """
        with caplog.at_level(logging.ERROR, logger="privacy_audit"):
            _lose(request_id="")

        lost = [r for r in caplog.records if "privacy_audit.record_lost" in r.getMessage()]
        assert len(lost) == 1
        assert f"request_id={observability.NO_CORRELATION_ID}" in lost[0].getMessage()


class TestTheLossAlerts:
    """Сигнал, который будит человека, — и не будит его двести раз подряд.

    Владелец назвал «alert при повторении или пороговом количестве», а не
    «alert на каждую». Различие не косметическое: сигнал на каждую потерю при
    длящейся аварии обучает его игнорировать.
    """

    @pytest.fixture
    def sentry_events(self, monkeypatch):
        import sentry_sdk

        captured = []
        monkeypatch.setattr(
            sentry_sdk, "capture_message",
            lambda message, level=None, **kw: captured.append((message, level)),
        )
        return captured

    def test_the_first_loss_alerts(self, broken_journal, sentry_events, settings):
        settings.PRIVACY_AUDIT_LOST_ALERT_EVERY = 10

        _lose()

        assert len(sentry_events) == 1, sentry_events
        message, level = sentry_events[0]
        assert "privacy_audit.record_lost" in message
        assert level == "error"

    def test_repetition_alerts_again_at_the_threshold_and_not_in_between(
        self, broken_journal, sentry_events, settings,
    ):
        settings.PRIVACY_AUDIT_LOST_ALERT_EVERY = 3

        for _ in range(7):
            _lose()

        # Первая — всегда; дальше каждая третья: 1, 3, 6.
        assert observability.LOST_RECORDS.total == 7
        assert len(sentry_events) == 3, sentry_events

    def test_the_alert_carries_the_correlation_id_and_the_count(
        self, broken_journal, sentry_events, settings,
    ):
        settings.PRIVACY_AUDIT_LOST_ALERT_EVERY = 10

        _lose(request_id="req-f6-trace-9")

        message, _level = sentry_events[0]
        assert "req-f6-trace-9" in message
        assert "lost_total=1" in message

    def test_the_alert_names_no_subject(self, broken_journal, sentry_events, settings):
        """152-ФЗ: событие уходит наружу, субъект — нет.

        Строка ERROR остаётся внутри контура и субъекта называет (так было и
        до F6). Событие Sentry покидает периметр, поэтому идентификатор
        человека в него не кладётся вовсе — не «скрабится потом», а не
        попадает.
        """
        settings.PRIVACY_AUDIT_LOST_ALERT_EVERY = 10
        subject = uuid.uuid4()

        _lose(subject=subject)

        message, _level = sentry_events[0]
        assert str(subject) not in message

    def test_a_broken_alert_channel_does_not_break_the_operation(
        self, broken_journal, monkeypatch, settings,
    ):
        """Заведомо ломающий вход, которого владелец не заказывал, но который
        решает смысл: наблюдение за отказом не имеет права стать вторым
        отказом. Иначе F6 вернул бы ровно то, что отменил."""
        settings.PRIVACY_AUDIT_LOST_ALERT_EVERY = 10
        import sentry_sdk

        def _explode(*_a, **_kw):
            raise RuntimeError("sentry transport is down")

        monkeypatch.setattr(sentry_sdk, "capture_message", _explode)

        assert _lose() == "lost"
        assert observability.LOST_RECORDS.total == 1


class TestWithoutSentry:
    """Условие владельца целиком: «SENTRY_DSN может быть не задан — это НЕ
    повод строить второй механизм».

    Значит счётчик и correlation id обязаны работать **без** Sentry, а не
    «работать через Sentry, когда он есть». Ниже Sentry убран радикально —
    самого пакета нет.
    """

    @pytest.fixture
    def no_sentry(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)

    def test_the_counter_still_counts_without_sentry(self, broken_journal, no_sentry):
        assert _lose() == "lost"

        assert observability.LOST_RECORDS.total == 1

    def test_the_correlation_id_still_reaches_the_log_without_sentry(
        self, broken_journal, no_sentry, caplog,
    ):
        with caplog.at_level(logging.ERROR, logger="privacy_audit"):
            _lose(request_id="req-f6-no-sentry")

        lost = [r for r in caplog.records if "privacy_audit.record_lost" in r.getMessage()]
        assert len(lost) == 1
        assert "request_id=req-f6-no-sentry" in lost[0].getMessage()

    def test_an_empty_dsn_is_not_a_second_mechanism(self, broken_journal, settings):
        """Пустой DSN — штатное состояние dev и CI (``.env.example``, ci.yml).

        Узел стоит здесь, чтобы «работает без Sentry» проверялось при той же
        настройке, при которой идёт CI, а не только при выломанном импорте.
        """
        settings.SENTRY_DSN = ""

        assert _lose() == "lost"
        assert observability.LOST_RECORDS.total == 1


@pytest.mark.django_db
class TestTheWholeWayThrough:
    """Один узел от HTTP до счётчика.

    Модульные узлы выше зовут ``record_or_lose`` напрямую и потому не
    доказывают, что боевой маршрут доносит correlation id. Этот — доказывает,
    и заодно повторяет то, ради чего F6 существует: человека обслужили.
    """

    RUNTIME_TOKEN = "test-runtime-internal-token"  # noqa: S105  # pragma: allowlist secret
    CTX_URL = "/api/v1/internal/users/{subject}/personal-context/"

    @pytest.fixture(autouse=True)
    def _tokens(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = self.RUNTIME_TOKEN
        return settings

    @pytest.fixture
    def alice(self):
        from users.models import User

        real = User.objects.create_user(
            username="f6_alice7001", password="x", role="client",  # pragma: allowlist secret
            phone="+79991177001",
        )
        external_id = "bot:telegram:37001"
        User.objects.create(
            username=external_id, role="client", is_proxy=True, is_guest=False,
            linked_user=real,
        )
        return real, external_id

    def test_a_self_read_is_served_and_the_loss_is_counted_under_its_request_id(
        self, alice, broken_journal, caplog,
    ):
        from rest_framework.test import APIClient

        alice_user, alice_id = alice
        client = APIClient()
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {self.RUNTIME_TOKEN}"
        client.defaults["HTTP_X_EXTERNAL_USER_ID"] = alice_id
        client.defaults["HTTP_X_REQUEST_ID"] = "req-f6-end-to-end"

        with caplog.at_level(logging.ERROR, logger="privacy_audit"):
            resp = client.get(self.CTX_URL.format(subject=alice_user.pk))

        # Ради этого вариант (б) и выбран: своя данность читается при
        # сломанном журнале.
        assert resp.status_code == 200, resp.content
        assert PersonalDataAccessLog.objects.count() == 0
        assert observability.LOST_RECORDS.total == 1

        lost = [r for r in caplog.records if "privacy_audit.record_lost" in r.getMessage()]
        assert len(lost) == 1, [r.getMessage() for r in caplog.records]
        assert "request_id=req-f6-end-to-end" in lost[0].getMessage()
