"""``urllib3`` закреплён, как и всё, что формирует текст наших ошибок (DRF-2027).

Предмет — **воспроизводимость**, а не защита какого-либо теста. Текст
``Max retries exceeded with url: …`` собирает ``urllib3``, и на нём стоит замер
DRF-2025 (``users/social_auth.py:103``, ``:131``). Пока версия не названа,
утверждение «замерено на версии X» не имеет смысла: один и тот же
``requirements.txt`` дал в CI **2.7.0** на прогоне по ``dev`` и **2.8.0** на
прогонах #487 и #489.

Узел **структурный**: он утверждает наличие пина, а не его значение и не версию
в окружении. Узел «версия в окружении равна пину» был бы красным у каждого с
несвежим venv и зелёным в CI — это предмет DRF-2023, отдельного листа.
"""

import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements.txt"
PINNED = re.compile(r"^urllib3==\d+\.\d+(\.\d+)?\s*(#.*)?$", re.MULTILINE)


def test_urllib3_is_pinned_like_everything_else_that_shapes_our_errors():
    text = REQUIREMENTS.read_text(encoding="utf-8")

    assert "urllib3" in text, (
        "urllib3 не упомянут в requirements.txt вовсе: версия, формирующая текст "
        "ошибки, определяется резолвером и меняется от прогона к прогону"
    )
    assert PINNED.search(text), (
        "urllib3 упомянут, но не закреплён через '==': замер формы сообщения "
        "нельзя привязать к версии"
    )
