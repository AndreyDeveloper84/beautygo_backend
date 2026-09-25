"""Ночной вывод пишет ровно два поля — и это половина границы риска (DRF-2431).

## Зачем узел существует

Строки, помеченные `conversational`, ночной проход вправе перезаписать:
`_SUBJECT_OWNED` — это `{"explicit", "erased"}`, и пометки разговора там нет.
Сам по себе этот факт звучит широко, но риск узок, и держат его **два**
независимых факта из **разных репозиториев**:

* здесь: вывод пишет **только** `favorite_masters` и `busy_days`;
* в боте: поток вопросов не умеет писать `favorite_masters` — и не умел
  никогда (`_FIELD_PARSERS`, коммит `7b55e828`).

Пересечение — **одно поле, `busy_days`**. Его и обсуждают с владельцем.

**Расширь любой из двух наборов — пересечение вырастет молча.** Узел ниже
держит здешнюю половину; вторую держит `test_ask_write_set_2431` в боте.
Один узел в одном репозитории соглашение двух не удержал бы.

## Что узел НЕ утверждает

Что перезапись — это правильно или неправильно. Выбор пути (оставить /
пометить / защитить) — за владельцем, и до его слова поведение не
меняется. Узел только не даёт границе разъехаться без ведома.
"""

from __future__ import annotations

import inspect

from users import personal_context_inference as inference

#: Поля, которые ночной вывод пишет сегодня. Список — часть границы риска
#: DRF-2431, а не деталь: см. докстроку модуля.
WRITTEN_BY_INFERENCE = {"favorite_masters", "busy_days"}


class TestTheNightlyPassWritesTwoFieldsAndNoMore:
    def test_the_write_set_has_not_grown(self) -> None:
        """Растёт набор — растёт и пересечение с тем, что человек называет сам."""
        source = inspect.getsource(inference)

        # Наличие раньше отсутствия: оба поля в модуле действительно есть,
        # иначе «ничего лишнего» было бы правдой и о пустом модуле.
        for field in WRITTEN_BY_INFERENCE:
            assert f'"{field}"' in source, field

        stamped = {
            name
            for name in ("favorite_masters", "busy_days", "diet_type",
                         "preferred_time_slots", "price_range_max",
                         "workplace_district", "home_district",
                         "min_rating_preference")
            if f"ctx.{name} =" in source
        }
        assert stamped == WRITTEN_BY_INFERENCE, (
            "Ночной вывод стал писать другое множество полей. Это половина "
            "границы DRF-2431: пересечение с полями, которые человек называет "
            "сам, выросло. Обновите обе половины и цену в листе."
        )

    def test_a_stated_answer_is_the_only_thing_it_refuses_to_touch(self) -> None:
        """Пометка разговора НЕ защищена — факт, на котором стоит лист.

        Узел покраснеет в тот день, когда `conversational` добавят в
        охраняемые. Это ожидаемо и правильно: такой шаг — выбор пути по
        DRF-2431, и он обязан быть замеченным, а не тихим.
        """
        assert inference._SUBJECT_OWNED == frozenset({"explicit", "erased"})
        assert "conversational" not in inference._SUBJECT_OWNED

    def test_the_favourites_list_is_rebuilt_whole(self) -> None:
        """Почему пометка «сказал сам» на этом поле была бы хуже перезаписи.

        Значение собирается целиком из истории записей, а не дописывается:
        замороженный пометкой список остался бы таким навсегда.
        """
        source = inspect.getsource(inference._infer_favorite_masters)

        assert "ctx.favorite_masters = inferred" in source
