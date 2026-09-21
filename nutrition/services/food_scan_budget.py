"""Бюджет распознавания фото — DRF-2145 (Сканер-1, решение владельца 20.09, В1).

До этого листа у сканера не было ни счётчика, ни бюджета: throttle
``food_scan_internal`` 60/мин защищает от всплеска, не от объёма, а расход
провайдера не измерялся вовсе. Здесь — два счётчика по дню (UTC): на человека
и общий, отказ по имени при исчерпании, сигнал на 80 / 100 % общего потолка
и стоимость из ``usage`` ответа провайдера.

### Правила

* **Попытка считается, не успех.** :func:`reserve` инкрементирует счётчики
  ДО вызова провайдера: упавший провайдер — тоже потраченный вызов (за
  него платят), и ложный вход «инкремент после провайдера» — красный
  (``test_food_scan_budget_2145`` b5).
* **Ключи по дню в UTC**, TTL — до полуночи: после полуночи счётчик пуст
  без чистки. Ключ человека — по ``user.pk``, не по внешнему id: публичный
  ``/nutrition/scan/`` внешнего id не несёт, а ``bot:<…>`` → тот же ``User``
  (отступление от буквы п.1 листа, названо в PR).
* **Отказ по личному потолку не тратит общий, и наоборот.** Проверка
  личного — первой (дешевле и адреснее); общий инкрементируется только
  когда личный пропустил.
* **Пороги — из листа** (§55): ``FOOD_SCAN_DAILY_PER_USER`` 20,
  ``FOOD_SCAN_DAILY_TOTAL`` 500, оба через env. Своих чисел здесь нет.
* **Счётчики живут в кэше, а кэш — вытесняемый** (Redis db 1 общий с
  остальным кэшем): ``cache.clear()``, FLUSHDB при выкладке или eviction
  по ``maxmemory`` обнуляют дневные счётчики и метки dedup сигнала (сигнал
  может прозвучать второй раз). Принятый риск: бюджет защищает дневник от
  разорения, а не деньги до цента.
* **Fail-open при недоступном кэше** (умолчание главного окна 20.09):
  бюджет защищает деньги, но потеря Redis не должна закрыть дневник; в лог
  — ``nutrition.food_scan.budget_unavailable``. ``django_redis`` с
  ``DJANGO_REDIS_IGNORE_EXCEPTIONS`` возвращает ``None`` вместо исключения —
  это читается так же: «счётчик не прочитать — пропустить».
* **Сигнал операторам.** Приёмника алертов у каталога нет —
  ``alerting.page`` с MAX-чатом операторов живёт в боте (DRF-2158).
  Здесь — структурная строка ``nutrition.food_scan.budget`` (WARNING на
  80 %, ERROR на 100 %) и Sentry capture по образцу
  :mod:`privacy_audit.observability` (никогда не бросает; без DSN — no-op),
  dedup по дню в кэше. Доставка операторам в MAX — предел, лист
  «каталог → бот». В сообщении — сумма стоимости за день (справка), без
  идентификаторов людей.
* **Текстовый путь** (``food-estimate``) бюджетом не ограничивается — он и
  провайдера не зовёт.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Пороги сигнала — из листа: 80 % (warning) и 100 % (error) общего потолка.
SIGNAL_THRESHOLDS: tuple[tuple[float, str], ...] = ((0.8, "warning"), (1.0, "error"))

_KEY_USER = "food_scan:user:{user_id}:{day}"
_KEY_TOTAL = "food_scan:total:{day}"
_KEY_COST = "food_scan:cost_usd:{day}"
_KEY_SIGNAL = "food_scan:signal:{day}:{level}"
#: DRF-2196 — у события свой ключ дедупа, а не общий с Sentry: при сбое
#: публикации его можно вернуть (следующий скан переспросит), не заставив
#: Sentry прозвучать второй раз. Дубликат события боту не страшен — у ядра
#: бота свой дедуп по суткам и порогу.
_KEY_EVENT = "food_scan:event:{day}:{level}"

#: Токены ``usage`` OpenAI, из которых складывается стоимость.
_USAGE_PROMPT = "prompt_tokens"
_USAGE_COMPLETION = "completion_tokens"
_ONE_MILLION = Decimal(1_000_000)


class BudgetRefusal(Exception):
    """Скан не пойдёт к провайдеру; ``code`` — код ответа API."""

    code = "FOOD_SCAN_BUDGET_REFUSED"

    def __init__(self, *, retry_after: int, limit: int, used: int) -> None:
        self.retry_after = retry_after
        self.limit = limit
        self.used = used
        super().__init__(f"{self.code} used={used} limit={limit}")

    @property
    def details(self) -> dict[str, Any]:
        return {"retry_after": self.retry_after, "limit": self.limit, "used": self.used}


class DailyLimitExceeded(BudgetRefusal):
    """Личный потолок за день — 429."""

    code = "FOOD_SCAN_DAILY_LIMIT"


class BudgetExhausted(BudgetRefusal):
    """Общий дневной потолок — 503."""

    code = "FOOD_SCAN_BUDGET_EXHAUSTED"


def _now() -> datetime:
    """Отдельная функция ради тестов (полночь, следующий день)."""
    return datetime.now(UTC)


def _day() -> str:
    return _now().date().isoformat()


def seconds_until_midnight() -> int:
    now = _now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def _limit(name: str, default: int) -> int:
    try:
        return max(0, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def _incr(key: str, ttl: int) -> int | None:
    """Инкремент с TTL до полуночи; ``None`` — кэш недоступен (fail-open)."""
    try:
        cache.add(key, 0, timeout=ttl)
        value = cache.incr(key)
    except ValueError:
        # ``incr`` на ключе, которого нет (кэш выключен / ключ не лёг): не
        # считаем — и говорим об этом тем же словом, что и про потерю кэша.
        logger.warning("nutrition.food_scan.budget_unavailable op=incr err=ValueError")
        return None
    except Exception as exc:  # noqa: BLE001 — потеря кэша не закрывает дневник
        logger.warning("nutrition.food_scan.budget_unavailable op=incr err=%s", type(exc).__name__)
        return None
    return int(value) if isinstance(value, int) else None


def _decr(key: str) -> None:
    try:
        cache.decr(key)
    except Exception:  # noqa: BLE001 — откат счётчика — лучшее усилие
        logger.warning("nutrition.food_scan.budget_unavailable op=decr")


def reserve(user: Any) -> None:
    """Занять одну попытку распознавания — ДО вызова провайдера.

    Raises:
        DailyLimitExceeded: личный потолок исчерпан (общий не тронут).
        BudgetExhausted: общий потолок исчерпан (личный откатывается).
    """
    day = _day()
    ttl = seconds_until_midnight()
    per_user = _limit("FOOD_SCAN_DAILY_PER_USER", 20)
    total_limit = _limit("FOOD_SCAN_DAILY_TOTAL", 500)

    user_key = _KEY_USER.format(user_id=getattr(user, "pk", user), day=day)
    used_by_user = _incr(user_key, ttl)
    if used_by_user is None:
        logger.warning("nutrition.food_scan.budget_unavailable scope=user — fail-open")
        return
    if used_by_user > per_user:
        _decr(user_key)
        raise DailyLimitExceeded(retry_after=ttl, limit=per_user, used=used_by_user - 1)

    total_key = _KEY_TOTAL.format(day=day)
    used_total = _incr(total_key, ttl)
    if used_total is None:
        logger.warning("nutrition.food_scan.budget_unavailable scope=total — fail-open")
        return
    if used_total > total_limit:
        _decr(total_key)
        _decr(user_key)
        _signal_if_crossed(used_total - 1, total_limit, day, ttl)
        raise BudgetExhausted(retry_after=ttl, limit=total_limit, used=used_total - 1)
    _signal_if_crossed(used_total, total_limit, day, ttl)


# ─── стоимость ────────────────────────────────────────────────────────────


def _price(name: str) -> Decimal | None:
    raw = getattr(settings, name, None)
    if raw in (None, ""):
        return None
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        logger.warning("nutrition.food_scan.price_invalid setting=%s", name)
        return None
    if price < 0:
        logger.warning("nutrition.food_scan.price_invalid setting=%s", name)
        return None
    return price


def cost_usd(usage: dict[str, Any] | None) -> Decimal | None:
    """Стоимость вызова из ``usage`` провайдера и цен в настройках.

    ``None`` — провайдер ``usage`` не отдал ИЛИ цены не заданы (в листе их
    нет; ``FOOD_SCAN_PRICE_INPUT_USD_PER_1M`` / ``…_OUTPUT_…`` — без
    умолчания). ``None``, не ноль: «не посчитано» ≠ «бесплатно».
    """
    if not isinstance(usage, dict) or not usage:
        return None
    price_in = _price("FOOD_SCAN_PRICE_INPUT_USD_PER_1M")
    price_out = _price("FOOD_SCAN_PRICE_OUTPUT_USD_PER_1M")
    if price_in is None or price_out is None:
        return None
    try:
        prompt = Decimal(int(usage.get(_USAGE_PROMPT) or 0))
        completion = Decimal(int(usage.get(_USAGE_COMPLETION) or 0))
    except (TypeError, ValueError):
        return None
    total = (prompt * price_in + completion * price_out) / _ONE_MILLION
    return total.quantize(Decimal("0.000001"))


def record_cost(amount: Decimal | None) -> None:
    """Суточная сумма — справка в сигнале. Лучшее усилие, без падений."""
    if amount is None:
        return
    key = _KEY_COST.format(day=_day())
    try:
        current = cache.get(key)
        new_total = (Decimal(str(current)) if current is not None else Decimal(0)) + amount
        cache.set(key, str(new_total), timeout=seconds_until_midnight())
    except Exception:  # noqa: BLE001
        logger.warning("nutrition.food_scan.budget_unavailable op=cost")


def _daily_cost_text(day: str) -> str:
    """Справка в сигнале; любая порча значения — «n/a», не исключение из
    ``reserve`` (сигнал никогда не важнее скана)."""
    try:
        current = cache.get(_KEY_COST.format(day=day))
        return f"{Decimal(str(current)):.4f}" if current is not None else "n/a"
    except Exception:  # noqa: BLE001 — InvalidOperation / потеря кэша
        return "n/a"


# ─── сигнал ───────────────────────────────────────────────────────────────


def _signal_if_crossed(used: int, limit: int, day: str, ttl: int) -> None:
    """80 / 100 % общего потолка — по одному сигналу в день на порог."""
    if limit <= 0:
        return
    for ratio, level in SIGNAL_THRESHOLDS:
        if used < limit * ratio:
            continue
        dedup_key = _KEY_SIGNAL.format(day=day, level=level)
        try:
            first = cache.add(dedup_key, 1, timeout=ttl)
        except Exception:  # noqa: BLE001
            first = False
        if not first:
            continue
        message = (
            f"nutrition.food_scan.budget level={level} used={used}/{limit} "
            f"ratio={used / limit:.2f} cost_usd={_daily_cost_text(day)} day={day}"
        )
        log = logger.error if level == "error" else logger.warning
        log(message)
        try:
            _send_signal(level, message)
        except Exception as exc:  # noqa: BLE001 — сигнал не важнее скана
            logger.warning("nutrition.food_scan.budget_signal_failed err=%s", type(exc).__name__)
        # DRF-2196 — событие уходит на том же пересечении, но со СВОИМ
        # ключом суток: при сбое публикации ключ возвращается, и следующий
        # скан переспросит. Иначе один сбой INSERT крал бы единственное
        # оповещение за сутки — ключ сигнала уже занят выше.
        _emit_budget_event_once(level=level, used=used, limit=limit, day=day, ttl=ttl)


def _emit_budget_event_once(*, level: str, used: int, limit: int, day: str, ttl: int) -> None:
    """Опубликовать событие не более одного раза за сутки на порог — и вернуть счёт при сбое.

    Никогда не бросает: сигнал не важнее скана. Отказ публикации пишет строку
    в лог и освобождает ключ суток, чтобы следующий скан попробовал снова.
    """
    key = _KEY_EVENT.format(day=day, level=level)
    try:
        first = cache.add(key, 1, timeout=ttl)
    except Exception:  # noqa: BLE001 — потеря кэша: молчание, а не шквал
        first = False
    if not first:
        return
    try:
        _emit_budget_event(level=level, used=used, limit=limit, day=day)
    except Exception as exc:  # noqa: BLE001 — сигнал не важнее скана
        logger.warning("nutrition.food_scan.budget_event_failed err=%s", type(exc).__name__)
        try:
            cache.delete(key)
        except Exception:  # noqa: BLE001 — возврат счёта — лучшее усилие
            logger.warning("nutrition.food_scan.budget_unavailable op=event_release")


def _daily_cost_or_none(day: str) -> str | None:
    """Суточная стоимость строкой для события, или ``None`` — «не посчитано».

    Не ``"n/a"``: ядро бота понимает ``None`` как «стоимость не посчитана» и
    говорит это оператору словами, а строку вывело бы буквально — «USD: n/a».
    """
    text = _daily_cost_text(day)
    return None if text == "n/a" else text


def _emit_budget_event(*, level: str, used: int, limit: int, day: str) -> None:
    """Сигнал бюджета — боту событием ``system.module.health.degraded`` (DRF-2196).

    До этого листа единственным «сигналом человеку» был Sentry, а на пилоте
    ``SENTRY_DSN`` пуст — то есть сигнала не было. Работающий канал к
    операторам — MAX — живёт в боте (``alerting.page``), и у бота есть ядро,
    которое решает, звучать ли странице. Вариант (а1), решение владельца §64:
    через outbox — HMAC, ретраи, dead-letter и ручной реплей уже есть.

    Конверт без пользователя и без тенанта: каталог считает снимки, а не
    тех, кто их прислал. В ``metric`` — только числа и сутки.

    Строка уходит боту только когда тема названа в
    ``OUTBOX_EXTERNAL_DELIVERY_TOPICS`` (флажок держит владелец, пока
    потребитель бота не зелёный «в оба конца»); иначе остаётся локальной.
    """
    # Локальный импорт: `envelope` тянет `appointments.models`, а этот модуль
    # грузится из `nutrition.views` при старте приложения.
    from django.db import transaction

    from appointments.infrastructure.outbox.envelope import emit_outbox_event

    # Savepoint. `emit_outbox_event` по докстрингу ждёт, что его зовут внутри
    # транзакции доменного изменения; у системного сигнала доменного
    # изменения нет, и сегодня скан-запрос не атомарен — INSERT коммитится
    # сразу. Но если `reserve()` когда-нибудь позовут внутри `atomic()`,
    # проглоченная выше `DatabaseError` без savepoint сломала бы внешнюю
    # транзакцию, и упал бы уже `scan.save()` — сигнал уронил бы скан.
    with transaction.atomic():
        emit_outbox_event(
            topic="system.module.health.degraded",
            data={
                "module_name": "nutrition.food_scan",
                "severity": level,
                # `metric` здесь объект; внутренние эмиттеры бота шлют его
                # строкой. Словарь бота требует только наличия ключа; две
                # формы у одного имени записаны в контракт (PR бота).
                "metric": {
                    "used": used,
                    "limit": limit,
                    "day": day,
                    "cost_usd": _daily_cost_or_none(day),
                },
            },
            actor="system",
            user_id=None,
            tenant_id=None,
        )


def _send_signal(level: str, message: str) -> bool:
    """Sentry — единственный «сигнал человеку» у каталога; без DSN — no-op.

    Доставка операторам в MAX (``alerting.page`` бота) — не отсюда: у
    каталога такого приёмника нет (предел DRF-2145, лист «каталог → бот»).
    """
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level=level)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("nutrition.food_scan.budget_signal_failed err=%s", type(exc).__name__)
        return False


__all__ = [
    "BudgetExhausted",
    "BudgetRefusal",
    "DailyLimitExceeded",
    "cost_usd",
    "record_cost",
    "reserve",
    "seconds_until_midnight",
]
