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

Почему команда начинается с того, КТО ей ответил
------------------------------------------------

11.09.2026 выяснилось, что машин две и совпадает у них всё, кроме адреса:
путь, учётка, имена compose-проектов. У брошенной копии контейнер БД
остался жив, когда прикладные вышли, — значит запрос туда не падает, он
**отвечает** правдоподобным числом из замороженного состояния. Это
опаснее отказа: отказ виден, а такой ответ выглядит как замер.

Правило «печатать хост рядом с числом» дыру не закрывает: **имя хоста —
это то, что я помню**, и неверный адрес называют честно. Поэтому блок
предмета печатает не имя, а то, что машина говорит о себе сейчас, —
время старта процесса БД и возраст последней записи
(`core/measurement_subject.py`).

`--max-age-hours` превращает это из строки для чтения в **проверку**:
пульс старше порога означает «мерю не ту машину», и выход ненулевой
(код 2, отдельный от кода 1 «геокодирование не готово»).
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai.application.services.recommendation_engine import RecommendationEngine
from core.measurement_subject import gather_pulse, newest, subject_lines
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
        parser.add_argument(
            "--max-age-hours", type=float, default=None,
            help=(
                "Код выхода 2, если последняя запись в контуре старше этого "
                "возраста: замороженная копия отвечает так же охотно, как "
                "живой пилот."
            ),
        )

    def handle(self, *args, **options) -> None:
        engine = RecommendationEngine()

        # Предмет печатается ПЕРВЫМ и до всякого счёта: число, у которого
        # не названо, кто его выдал, читателю не нужно.
        #
        # Пульс собирается один раз и отдаётся обоим: печати и проверке.
        # Два сбора дали бы напечатанный возраст и проверенный возраст из
        # разных мгновений, и расхождение осталось бы незамеченным.
        pulses = gather_pulse()
        for line in subject_lines(pulses=pulses):
            self.stdout.write(line)
        self._check_freshness(pulses, options["max_age_hours"])
        self.stdout.write("")

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

    def _check_freshness(self, pulses, max_age_hours) -> None:
        """Пульс старше порога значит «мерю не ту машину».

        Отдельный код выхода (2, а не 1) намеренно: «геокодирование не
        готово» — новость про данные, «замер не с той машины» — новость
        про то, что предыдущая строка вообще ничего не значит. Скрипт,
        различающий их по коду, не станет чинить второе как первое.

        Порог не имеет умолчания. Живой контур с редким трафиком молчит
        сутками законно, и подставленное здесь число превращало бы тишину
        в обвинение. Пока порог не назван, возраст — строка для чтения.
        """
        if max_age_hours is None:
            return

        freshest = newest(pulses)
        limit = timedelta(hours=max_age_hours)
        if freshest is None:
            self.stderr.write(self.style.ERROR(
                "СВЕЖЕСТЬ НЕ ПОДТВЕРЖДЕНА: ни одна опора не ответила. "
                "Это не «база пустая» и не «всё хорошо» — это отсутствие "
                "ответа на вопрос, та ли машина."
            ))
            raise SystemExit(2)

        age = timezone.now() - freshest.at
        if age > limit:
            self.stderr.write(self.style.ERROR(
                f"НЕ ТА МАШИНА (или контур стоит): последняя запись — "
                f"{freshest.at.isoformat()} ({freshest.label}), это старше "
                f"порога {max_age_hours} ч. Числа ниже недействительны."
            ))
            raise SystemExit(2)

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
