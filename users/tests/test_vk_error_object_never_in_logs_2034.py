"""DRF-2034: объект ошибки провайдера не пишется в лог целиком.

**Довод — предупредительный, и это названо нарочно.** Исходно он звучал
сильнее: «в ответах VK встречается ``request_params``, эхом повторяющий
отправленное». Это было **знание, а не замер**: в каталоге ``request_params``
ноль вхождений, в боте — локальная переменная без отношения к VK, фикстуры
проверяют наш собственный конверт. Утверждение снято.

Остаётся утверждение **об устройстве**, и оно не требует подтверждения от
провайдера: ``data["error"]`` — **чужой объект, форму которого задаём не мы**.
Сегодня в нём три поля, завтра провайдер добавит четвёртое, и **наш лог
изменится без единой правки с нашей стороны**. Это неограниченная поверхность
**по построению**, а не по содержимому.

**Отсюда устройство теста.** Он вкладывает в payload поле, которого мы не
знаем, и требует, чтобы оно не доехало до лога. Тест на конкретное имя
(``request_params``) вернул бы через заднюю дверь ровно то утверждение,
которое я снял, и протух бы, поменяй VK своё поведение. Проверяется свойство
«в лог попадают только НАЗВАННЫЕ нами поля», а не «такого-то поля там нет».

**Достижимость.** Путь «неверный токен» достижимее сетевого сбоя из
``:103``: токены истекают и отзываются чаще, чем рвётся связь.

Перехват — на **именованный** логгер: ``users`` объявлен ``propagate=False``
(`settings/base.py`), корневой обработчик записей ``users.social_auth`` не
увидит, и пустой перехват прошёл бы все утверждения «значения нет».
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from users import social_auth

LOGGER_NAME = "users.social_auth"

#: Поле, которого мы не знаем. Не секрет и не имя из VK — маркер того,
#: что форма чужая и может прирасти в любой момент.
UNKNOWN_FIELD = "some_future_field_we_do_not_know_about"
UNKNOWN_VALUE = "UnknownPayloadMarker2034"


@contextmanager
def _capturing(caplog):
    target = logging.getLogger(LOGGER_NAME)
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            yield
    finally:
        target.removeHandler(caplog.handler)


def _vk_error_payload(monkeypatch):
    """VK отвечает 200 с объектом ошибки — путь `if "error" in data`."""

    class _Resp:
        def json(self):
            return {
                "error": {
                    "error_code": 5,
                    "error_msg": "User authorization failed: invalid access_token.",
                    UNKNOWN_FIELD: UNKNOWN_VALUE,
                }
            }

    monkeypatch.setattr(social_auth.requests, "get", lambda *a, **k: _Resp())


def _text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


class TestVkErrorObject:
    """`verify_vk_token` — ветка «провайдер вернул объект ошибки»."""

    def test_unknown_fields_are_not_logged(self, caplog, monkeypatch):
        """Главный узел: незнакомое поле не доезжает до лога.

        Сегодня печатается весь словарь, поэтому доезжает любое поле —
        включая то, которого ещё нет.
        """
        _vk_error_payload(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        captured = _text(caplog)
        assert captured, "перехват пуст — утверждения ниже ничего не проверяют"
        assert UNKNOWN_FIELD not in captured
        assert UNKNOWN_VALUE not in captured

    def test_the_error_code_is_named(self, caplog, monkeypatch):
        """Положительная стража: диагностируемость обязана остаться.

        Без неё «убрать всё» прошло бы как починка, и отказ провайдера
        стал бы неотличим от его отсутствия.
        """
        _vk_error_payload(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        assert "error_code=5" in _text(caplog)

    def test_the_error_message_is_named(self, caplog, monkeypatch):
        """Сообщение провайдера — названное поле, а не весь объект."""
        _vk_error_payload(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        captured = _text(caplog)
        assert "error_msg=" in captured
        assert "User authorization failed" in captured

    def test_a_non_mapping_error_is_not_logged_verbatim(self, caplog, monkeypatch):
        """Узел заведён ПОСЛЕ первого красного — и вот почему, честно.

        Очевидная правка — ``err.get("error_code")``. Но довод этого листа
        гласит: **форму чужого объекта задаём не мы**. Значит нельзя
        предполагать и то, что объект вообще является словарём: придёт
        строка — ``.get`` бросит ``AttributeError``, и чистый
        ``SocialAuthTokenError`` превратится в 500. Сегодняшний ``%s``
        такого не делает, то есть минимальная правка **вносила бы** отказ,
        которого нет, — на том самом допущении, которое лист запрещает.

        Поэтому набор переобъявлен (5 узлов, 4 красных) и красный переснят
        на неправленом исходнике: инструмент изменён — значит прежний
        красный к нему не относится.
        """

        class _Resp:
            def json(self):
                return {"error": f"plain string error carrying {UNKNOWN_VALUE}"}

        monkeypatch.setattr(social_auth.requests, "get", lambda *a, **k: _Resp())
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        captured = _text(caplog)
        assert captured, "перехват пуст — утверждения ниже ничего не проверяют"
        assert UNKNOWN_VALUE not in captured

    def test_the_capture_is_not_empty(self, caplog, monkeypatch):
        """Сторож пустого перехвата — ОБЪЯВЛЕН ЗЕЛЁНЫМ до правки."""
        _vk_error_payload(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        assert _text(caplog).strip(), (
            "перехват пуст: users.* объявлен propagate=False, "
            "и подвеска на корень записей не видит"
        )
