"""Замер достижимости на реальном каталоге — T5 (DRF-1566).

Одна команда, воспроизводимое число, никакого трафика и никакого PII::

    python manage.py recommendation_reachability --k 3

Baseline снимается **до** миграций потребителей: доказывать «стало не хуже»
после T6/T9/T12 будет не с чем, если сегодняшнего числа нет. Замер, не
попавший в отчёт задачи, не существует — вывод команды кладётся в DRF-1566.

Что считается
-------------
Два порядка над **одним и тем же** множеством кандидатов:

* `fixed_tie_break` — сегодняшний домашний экран: балл по рейтингу, ничья
  по `str(id)`, срез в три. Порядок один на всех людей, поэтому всё, что
  за позицией `k`, не видит никто и никогда;
* `rotation_within_tier` — модель резолвера: ярусы, ротация внутри яруса.
  Недостижим только тот, над кем уже `k` кандидатов в строго старших ярусах.

Разница поимённо — это люди, которых сегодня не видит никто, а после
границы увидит кто-то.

Почему формула сегодняшнего экрана берётся ИМПОРТОМ
---------------------------------------------------
`_compute_layer_2_score` импортируется из `users.catalog_recommendations_api`,
а не переписывается здесь. Переписанная формула мерила бы не то, что
работает на контуре, а то, что мы про неё помним, — и первая же правка
развела бы замер с предметом замера. Когда T6 удалит формулу, эта команда
перестанет импортироваться, и это правильный сигнал: baseline снимают до
миграции, а не после.
"""
from __future__ import annotations

import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from recommendation._reachability import (
    exposure_gap,
    unreachable_with_fixed_order,
    unreachable_with_rotation,
)


class Command(BaseCommand):
    help = "Аналитическая достижимость кандидатов: кого не видит никто (T5, DRF-1566)"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--k", type=int, default=3,
            help="сколько кандидатов ПОКАЗЫВАЕТСЯ поверхностью (по умолчанию 3 — LAYER_2_LIMIT)",
        )
        parser.add_argument(
            "--json", action="store_true",
            help="машинный вывод: результат кладётся в отчёт задачи как есть",
        )

    def handle(self, *args, **options) -> None:
        k = options["k"]
        if k < 1:
            raise CommandError("k — сколько будет показано; k < 1 не является запросом")

        rows = self._load_pool()
        if not rows:
            # Пустой пул — не ноль недостижимых, а отсутствие замера.
            # Разница та же, что между «никого нет» и «мы не искали».
            raise CommandError(
                "пул кандидатов пуст: замер не снят. Это НЕ «все достижимы» — "
                "проверьте, что команда идёт против контура с данными"
            )

        fixed = unreachable_with_fixed_order([cid for cid, _ in self._fixed_order(rows)], k=k)
        rotation = unreachable_with_rotation(self._tiers(rows), k=k)
        gap = exposure_gap(rotation=rotation, fixed=fixed)

        if options["json"]:
            self.stdout.write(json.dumps({
                "k": k,
                "pool_size": len(rows),
                "fixed_tie_break": {
                    "unreachable_count": fixed.unreachable_count,
                    "unreachable": [str(x) for x in fixed.unreachable],
                },
                "rotation_within_tier": {
                    "unreachable_count": rotation.unreachable_count,
                    "unreachable": [str(x) for x in rotation.unreachable],
                },
                "exposure_gap": [str(x) for x in gap],
            }, ensure_ascii=False, indent=2))
            return

        self.stdout.write(f"пул: {len(rows)} бронируемых мастеров в активных салонах, k={k}")
        self.stdout.write(fixed.summary())
        self.stdout.write(rotation.summary())
        self.stdout.write(
            f"цена детерминированной ничьи: {len(gap)} мастеров сегодня не видит никто, "
            f"а при ротации внутри яруса увидит кто-то"
        )
        for cid in gap:
            self.stdout.write(f"  {cid}")

    # -- загрузка пула -----------------------------------------------------

    def _load_pool(self) -> list:
        """Доменная допустимость, не политика: активен, бронируем, салон жив.

        Те же условия, что у `_base_pool`, и намеренно только они: любое
        дополнительное условие здесь было бы уже отбором, то есть замер
        мерил бы наш собственный фильтр.
        """
        from users.models import SpecialistProfile

        return list(
            SpecialistProfile.objects
            .filter(
                is_available=True,
                is_booking_enabled=True,
                status=SpecialistProfile.ProfileStatus.ACTIVE,
                tenant__is_active=True,
            )
            .only("id", "rating", "reviews_count", "is_available")
        )

    # -- два порядка над одним множеством ----------------------------------

    def _fixed_order(self, rows) -> list[tuple[str, float]]:
        """Сегодняшний порядок домашнего экрана, дословно.

        После T6 формулы больше нет — она упразднена вместе с авторитетом
        поверхности. Тогда эта половина замера не «даёт ноль», а
        становится **неизмеримой**, и команда обязана сказать именно это:
        baseline «до» снимают ДО миграции, и если его не сняли, задним
        числом он не берётся.
        """
        try:
            from users.catalog_recommendations_api import _compute_layer_2_score
        except ImportError as exc:
            raise CommandError(
                "формула домашнего экрана удалена (T6/DRF-1567) — половина замера «до» "
                "больше не вычислима. Это не ноль недостижимых, это отсутствие замера. "
                "Записанный baseline ищите в DRF-1566"
            ) from exc

        scored = [(row, _compute_layer_2_score(row, lat=None, lon=None)) for row in rows]
        scored.sort(key=lambda pair: (-pair[1], str(pair[0].id)))
        return [(str(row.id), score) for row, score in scored]

    def _tiers(self, rows) -> dict[str, int]:
        """Ярусы резолвера на сегодняшних данных.

        S5 (качество) для кандидата с `reviews_count = 0` **неактивна**, а
        стадия, неактивная хотя бы для одного кандидата группы, не различает
        никого в этой группе (§29.4 «не понижать»). На пилоте отзывов нет
        ни у одного мастера, значит ярус ровно один — и это не упрощение
        модели, а её честный ответ на сегодняшние данные.
        """
        from recommendation.api import RatingValue, rating_strength
        from recommendation.api import EvidenceStrength

        confirmed = {
            str(row.id): row
            for row in rows
            if row.rating is not None
            and rating_strength(RatingValue(Decimal(row.rating), row.reviews_count))
            is EvidenceStrength.CONFIRMED
        }
        if len(confirmed) < len(rows):
            # Хотя бы один кандидат без подтверждённого свидетельства —
            # стадия молчит про всю группу, ярус один.
            return {str(row.id): 1 for row in rows}

        by_rating = sorted({float(row.rating) for row in rows}, reverse=True)
        rank_of = {value: index + 1 for index, value in enumerate(by_rating)}
        return {str(row.id): rank_of[float(row.rating)] for row in rows}
