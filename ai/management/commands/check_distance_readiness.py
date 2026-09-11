"""Готов ли подбор различать мастеров по расстоянию — числом, на живых данных.

Зачем команда, а не тест
------------------------

Тест живёт в **своей** базе и сам задаёт себе данные. Он способен
проверить, правильно ли ведёт себя код при заданных координатах
(`ai/tests/test_distance_stand.py`), и **не способен** ответить, есть ли
координаты там, где лежат настоящие мастера. Между тестом и этим
предметом нет связи: задашь координаты в фикстуре — он зелен всегда;
не задашь — красен всегда, включая день после геокодирования.

Здесь предмет — **состояние базы**, поэтому и инструмент читает базу.

Что это за число и зачем оно перевернётся само
----------------------------------------------

Замер 11.09.2026 (`docs/MEASURE_DISTANCE_FILTERING.md`): на пилоте
**31 мастер, 0 с координатами**, значит `_score_distance` возвращает
`0.5` всем и расстояние не различает никого — при весе в четверть балла.

```
сегодня          с координатами 0    различных баллов 1
после геокодера  с координатами N    различных баллов > 1
```

Команда — приёмка геокодирования: пока второе не наступило, работа не
сделана, сколько бы адресов ни было разобрано.

Предупреждение, которое стоит в замере первой строкой и повторено здесь
----------------------------------------------------------------------

**Геокодировать всех или никого.** Нейтральное значение `0.5` — середина
шкалы, а не край: геокодированный мастер дальше половины порога
проигрывает мастеру, про которого не известно ничего. Частичный прогон
создаёт направленную ложь там, где её сейчас нет. Поэтому команда
печатает **и всего, и с координатами** — разрыв между ними и есть мера
опасности.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai.application.services.recommendation_engine import RecommendationEngine
from users.models import SpecialistProfile


class Command(BaseCommand):
    help = "Сколько мастеров имеют координаты и различает ли их подбор по расстоянию."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--from-lat", type=float, default=None,
            help="Широта точки отсчёта. Нужна, только когда координаты уже есть.",
        )
        parser.add_argument(
            "--from-lon", type=float, default=None,
            help="Долгота точки отсчёта.",
        )
        parser.add_argument(
            "--require-ready", action="store_true",
            help="Ненулевой код выхода, если координаты есть не у всех.",
        )

    def handle(self, *args, **options) -> None:
        engine = RecommendationEngine()
        # `all_tenants` не нужен: профили специалистов глобальны. Если это
        # изменится, счёт молча схлопнется до одного салона — и строка
        # «предмет» ниже это покажет числом `всего`.
        rows = list(
            SpecialistProfile.objects.values_list("location_lat", "location_lng")
        )
        total = len(rows)
        with_coords = sum(
            1 for lat, lng in rows if lat is not None and lng is not None
        )

        # Предмет рядом с результатом: ноль без предмета неотличим от
        # «посчитали не то». Пустая таблица даёт те же нули, что и
        # отсутствие координат, и это разные новости.
        self.stdout.write(
            f"предмет: {timezone.now().isoformat(timespec='seconds')} · "
            f"порог max_distance_km={engine._max_distance_km}"
        )
        self.stdout.write(f"специалистов всего          : {total}")
        self.stdout.write(f"из них с координатами       : {with_coords}")

        if total == 0:
            self.stdout.write(self.style.WARNING(
                "таблица пуста — числа ниже не о готовности, а об отсутствии данных"
            ))
            return

        distinct = self._distinct_scores(
            engine, rows, options["from_lat"], options["from_lon"],
        )
        self.stdout.write(f"различных баллов расстояния : {distinct}")

        if with_coords == 0:
            self.stdout.write(self.style.WARNING(
                "расстояние не различает никого: координат нет ни у кого. "
                "Вес в четверть балла сегодня константа."
            ))
        elif with_coords < total:
            self.stdout.write(self.style.ERROR(
                f"ЧАСТИЧНАЯ ГЕОКОДИРОВКА: {with_coords} из {total}. "
                "Это хуже её отсутствия — нейтральное значение 0.5 есть середина "
                "шкалы, и негеокодированные получают преимущество или штраф по "
                "признаку, которого у них нет. Геокодировать всех или никого."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "координаты есть у всех — частичной лжи нет"
            ))

        if options["require_ready"] and with_coords < total:
            raise SystemExit(1)

    def _distinct_scores(self, engine, rows, from_lat, from_lon) -> str:
        """Сколько различных баллов расстояния даёт сегодняшний каталог.

        Когда координат нет ни у кого, ответ **не зависит от точки
        отсчёта**: `_distance_to` возвращает `None` при отсутствии любой
        из четырёх координат, значит балл один и тот же для любого
        клиента. Тогда точку требовать незачем, и мы её не требуем.

        Как только координаты появились, число зависит от того, откуда
        смотреть, — и выдумывать эту точку нельзя: «центр города»,
        подставленный молча, дал бы правдоподобное число ни о чём.
        """
        has_any = any(lat is not None and lng is not None for lat, lng in rows)
        if not has_any:
            return "1 (не зависит от точки отсчёта: координат нет ни у кого)"

        if from_lat is None or from_lon is None:
            raise CommandError(
                "координаты у части мастеров уже есть — укажите точку отсчёта "
                "--from-lat/--from-lon. Подставлять её молча нельзя: число "
                "получилось бы правдоподобным и ни о чём."
            )

        scores = {
            engine._score_distance(
                _haversine_or_none(from_lat, from_lon, lat, lng)
            )
            for lat, lng in rows
        }
        return str(len(scores))


def _haversine_or_none(from_lat, from_lon, lat, lng):
    """Расстояние или `None` — той же логикой, что и у движка.

    Дубль условия намеренный и узкий: `_distance_to` принимает
    `SpecialistProfile` и `RecommendationQuery`, а здесь на руках две
    пары чисел. Дублируется **условие про `None`**, а сам расчёт берётся
    у движка, чтобы формула осталась одна.
    """
    if lat is None or lng is None:
        return None
    from ai.application.services.recommendation_engine import _haversine_km

    return _haversine_km(from_lat, from_lon, float(lat), float(lng))
