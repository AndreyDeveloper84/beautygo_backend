"""Потеря записи журнала, сделанная видимой для машины — owner F6, 16.09.2026.

Решение владельца дословно: обычное чтение человеком **своих** данных не
останавливается из-за временного отказа записи аудита. Операция продолжается,
``privacy_audit.record_lost`` фиксируется — и дальше начинается то, ради чего
существует этот модуль::

    Если ERROR никто не читает и нет metric/alert — вариант (б) НЕ считается
    полностью реализованным.

До F6 потеря была строкой в логе и больше ничем. Строка отвечает на «что
случилось» и молчит на «сколько раз» и «с каким запросом»: пустая консоль и
перезапущенный контейнер выглядят одинаково, а ERROR без correlation id не с
чем сопоставить. Здесь добавлены ровно три недостающие величины:

* **счётчик**, который можно спросить (``LOST_RECORDS``), а не грепать;
* **correlation id** — тот же ``request_id``, который ушёл бы в строку
  журнала, так что потеря связывается с остальным следом запроса;
* **сигнал** в Sentry при первой потере и дальше по порогу — «при повторении
  или пороговом количестве», как сказал владелец, а не на каждую.

### Почему счётчик в процессе, а не в таблице

Считать потери в базе — значит писать в базу ровно тогда, когда база и
отказала. Счётчик обязан пережить ту аварию, о которой он сообщает, поэтому
он в памяти процесса.

**Цена названа, а не умолчана:** значение обнуляется при перезапуске и живёт
отдельно в каждом воркере. Поэтому ``LOST_RECORDS.total == 0`` доказывает
«в этом процессе с его старта потерь не было» и НЕ доказывает «потерь не
было». Величина, по которой считают за сутки и по всем воркерам, — это
поток строк ``privacy_audit.record_lost`` в сборщике логов (в проде формат
JSON, поля именованы) и события Sentry. Метрик-бэкенда в репозитории нет, и
заводить его ради этого решения владелец не просил.

### Почему сигнал не обязателен, а счётчик обязателен

``SENTRY_DSN`` пуст в dev и в CI (``.env.example``, ``ci.yml``) — тогда SDK
no-op. Это штатное состояние, а не поломка, и **второго механизма на этот
случай нет**: счётчик и correlation id не знают про Sentry вовсе, а отправка
события обёрнута так, что её отсутствие, отказ или сам отсутствующий пакет
ничего не меняют для человека, которого обслуживают. Наблюдение за отказом
не имеет права стать вторым отказом.

### Что сюда НЕ переехало

Решение F6 названо владельцем «только для ordinary self-read» и «не
распространяется автоматически на consequential operations». Развилка
«останавливать или пропускать» осталась там, где была, —
:func:`privacy_audit.policy.stops_when_unauditable`. Этот модуль зовётся
**после** того, как развилка уже пропустила операцию, и поэтому не может
сдвинуть границу: отказавший экспорт сюда не приходит и в счётчик потерь не
попадает.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from django.conf import settings

from core.log_filters import NO_REQUEST_SENTINEL, get_request_id

logger = logging.getLogger("privacy_audit")

#: «Никто не назвал correlation id». Одно написание с тем, что подставляет
#: ``core.log_filters`` в каждую строку лога — иначе отсутствие выглядело бы
#: по-разному в соседних полях одной записи.
NO_CORRELATION_ID = NO_REQUEST_SENTINEL

#: Порог повторения: первая потеря сигналит всегда, дальше каждая N-я.
ALERT_EVERY_SETTING = "PRIVACY_AUDIT_LOST_ALERT_EVERY"
DEFAULT_ALERT_EVERY = 10


class LostRecordCounter:
    """Сколько записей журнала потеряно в этом процессе, всего и по операциям.

    Блокировка не украшение: Django обслуживает запросы параллельно, а
    ``+= 1`` на счётчике потерь, который теряет инкременты, — счётчик,
    занижающий ровно то, ради чего заведён.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._by_operation: dict[str, int] = {}

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def by_operation(self) -> dict[str, int]:
        """Копия, а не внутренний словарь: счётчик читают, а не правят."""
        with self._lock:
            return dict(self._by_operation)

    def observe(self, operation: str) -> int:
        """Засчитать одну потерю и вернуть НОВЫЙ итог.

        Итог возвращается, а не вычитывается следом отдельным ``total``:
        между инкрементом и чтением успевает вклиниться соседний поток, и
        строка лога назвала бы чужое число.
        """
        with self._lock:
            self._total += 1
            self._by_operation[operation] = self._by_operation.get(operation, 0) + 1
            return self._total

    def reset(self) -> None:
        """Только для тестов: в бою обнулять счётчик потерь нечем и незачем."""
        with self._lock:
            self._total = 0
            self._by_operation = {}


#: Счётчик процесса. Смотреть вместе с потоком строк ``record_lost``, а не
#: вместо него — см. «Почему счётчик в процессе» выше.
LOST_RECORDS = LostRecordCounter()


@dataclass(frozen=True)
class LostRecordNote:
    """Что зафиксировано о потере — для строки лога, которую пишет вызывающий."""

    total: int
    correlation_id: str
    alerted: bool


def correlation_id_for(request_id: str = "") -> str:
    """Id запроса, под которым потеря связывается с остальным следом.

    Значение вызывающего, иначе thread-local, который наполняет
    ``RequestIDMiddleware``, иначе ``"-"``. Правдоподобного значения здесь
    не появляется никогда: подставленный id склеил бы потерю с чужим
    запросом, а «никто не назвал» и «вот значение» обязаны остаться
    различимы — тот же довод, по которому ``basis`` в журнале оставлен
    пустым.
    """
    stated = (request_id or "").strip()
    if stated:
        return stated
    return get_request_id() or NO_CORRELATION_ID


def alert_every() -> int:
    """Каждая N-я потеря сигналит. Кривая настройка не выключает сигнал.

    Ноль, отрицательное или не число означали бы «не сигналить никогда» —
    то есть тихое отключение единственного, что будит человека, из-за
    опечатки в окружении. Поэтому такое значение отвергается вслух и
    заменяется умолчанием.
    """
    raw = getattr(settings, ALERT_EVERY_SETTING, DEFAULT_ALERT_EVERY)
    try:
        every = int(raw)
    except (TypeError, ValueError):
        every = 0
    if every < 1:
        logger.warning(
            "privacy_audit.alert_threshold_misconfigured setting=%s value=%r "
            "— сигнал не выключается опечаткой, взято умолчание %d",
            ALERT_EVERY_SETTING, raw, DEFAULT_ALERT_EVERY,
        )
        return DEFAULT_ALERT_EVERY
    return every


def _should_alert(total: int, every: int) -> bool:
    """Первая потеря — всегда; дальше по порогу.

    Первая, потому что «журнал перестал писаться» — событие само по себе.
    Дальше по порогу, потому что сигнал на каждую потерю при длящейся аварии
    учит человека его игнорировать, и следующий настоящий сигнал утонет.
    """
    return total == 1 or total % every == 0


def _send_alert(message: str) -> bool:
    """Отправить событие туда, где оно разбудит человека. Никогда не бросить.

    Sentry настроен в ``djangoProject/settings/base.py`` и активен только при
    заданном ``SENTRY_DSN``; при пустом DSN вызов — no-op, и это штатное
    состояние dev и CI. Импорт локальный и обёрнут целиком: отсутствующий
    пакет, неинициализированный SDK и лежащий транспорт — три разных повода
    промолчать и ни одного повода уронить операцию, которую владелец велел
    не останавливать.
    """
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level="error")
        return True
    except Exception as exc:  # noqa: BLE001 — сигнал не важнее операции
        logger.warning(
            "privacy_audit.record_lost_alert_failed err=%s — потеря записи "
            "зафиксирована в счётчике и в строке ERROR выше",
            exc.__class__.__name__,
        )
        return False


def note_record_lost(
    *,
    operation: str,
    result: str,
    actor_named,
    request_id: str = "",
) -> LostRecordNote:
    """Засчитать потерю, получить correlation id, при необходимости сигналить.

    Строку ``privacy_audit.record_lost`` пишет вызывающий
    (:func:`privacy_audit.services.record_or_lose`) — там же, где она писалась
    до F6, и с тем же именем события: сборщики логов и глаза настроены на
    него, а переезд имени стоил бы дороже, чем даёт.

    Субъект сюда не передаётся вовсе. Он остаётся в строке ERROR внутри
    контура; наружу, в событие, идентификатор человека не кладётся — не
    «вычищается потом», а не попадает.
    """
    total = LOST_RECORDS.observe(operation)
    correlation_id = correlation_id_for(request_id)

    alerted = False
    if _should_alert(total, alert_every()):
        alerted = _send_alert(
            "privacy_audit.record_lost "
            f"operation={operation} result={result} actor_named={actor_named} "
            f"request_id={correlation_id} lost_total={total} "
            "— журнал доступа недоступен, операция выполнена без записи (§107, F6)"
        )
    return LostRecordNote(
        total=total, correlation_id=correlation_id, alerted=alerted,
    )
