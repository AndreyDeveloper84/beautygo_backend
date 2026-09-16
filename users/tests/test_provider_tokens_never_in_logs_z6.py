"""Z6: токен провайдера не уходит в лог при сетевом сбое.

**Предмет.** `requests` кладёт в текст исключения полный URL **вместе с
query** (`Max retries exceeded with url: /method/users.get?access_token=…`).
Поэтому `logger.warning("VK API error: %s", e)` печатает `access_token`
пользователя, а `logger.warning("Google API error: %s", e)` — `id_token`.
Оба места существуют с 22.03.2026 (`606f031`).

**Ни одного настоящего секрета здесь нет.** В исключение кладётся
синтетический маркер, изготовленный самим тестом; утверждения проверяют
**отсутствие по форме** — что в логе нет ни имени параметра, ни query, ни
длинного непрозрачного прогона, — и отдельно отсутствие маркера.

**Почему маркер внутри исключения, а не сравнение с текстом urllib3.**
Точная формулировка «Max retries exceeded with url: …» принадлежит
urllib3, который у нас **не пинован**: тест на его дословный текст
сломается на обновлении и будет проверять чужую библиотеку. Здесь
проверяется НАША обязанность: что бы ни лежало в исключении, оно не
должно попасть в лог целиком. Маркер — способ это увидеть.

**Ловушка, из-за которой проба может пройти вхолостую.** У VK ловушка
`except (requests.RequestException, ValueError)` — одна на две причины.
`ValueError` (не разобрался JSON) URL **не несёт**, и проба по нему
зелёная и до правки. Поэтому ветка `ValueError` объявлена зелёной
заранее и держится отдельным узлом: она сторожит, что мы не выдали
зелень по нетекущей ветке за доказательство.

**Перехват вешается на ИМЕНОВАННЫЙ логгер.** `users` объявлен
`propagate=False` (`settings/base.py:1337`), корневой обработчик
`caplog` записей `users.social_auth` не увидит, и пустой перехват прошёл
бы все утверждения «значения нет», не проверив ничего.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import pytest
import requests

from users import social_auth

LOGGER_NAME = "users.social_auth"

#: Синтетический маркер. Не секрет: изготовлен тестом, нигде не хранится.
VK_MARKER = "vkAccessTokenMarkerZ6AAAAAAAAAAAAAAAA"
GOOGLE_MARKER = "googleIdTokenMarkerZ6BBBBBBBBBBBBBBBB"

#: «Непрозрачный прогон» — форма токена, а не значение: ≥24 символов, и в нём
#: есть цифра и заглавная буква.
#:
#: Первая редакция запрещала любой идентификатор ≥20 символов из
#: ``[A-Za-z0-9_\-.]`` — и запрещала тем самым наши собственные имена событий:
#: ``google.transport_failed`` это 23 символа. Причём ``vk.transport_failed``
#: (19) проходил, то есть страж отверг бы одно место и пропустил другое **по
#: причине, не связанной с утечкой**. Дефект найден попыткой удовлетворить
#: собственного стража, а не его срабатыванием, — поэтому назван здесь, а не
#: молча исправлен.
_RUN = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _opaque_runs(text: str) -> list[str]:
    """Прогоны, похожие на токен: длинные И с цифрой И с заглавной."""
    return [
        run
        for run in _RUN.findall(text)
        if any(c.isdigit() for c in run) and any(c.isupper() for c in run)
    ]


@contextmanager
def _capturing(caplog):
    target = logging.getLogger(LOGGER_NAME)
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            yield
    finally:
        target.removeHandler(caplog.handler)


def _transport_error(url_with_query: str) -> requests.ConnectionError:
    """Сбой сети в той форме, в какой его отдаёт `requests`.

    Форма воспроизведена, а не процитирована: важно, что текст исключения
    несёт URL с query, а не то, какими словами urllib3 это описывает.
    """
    return requests.ConnectionError(
        f"HTTPSConnectionPool(host='example.invalid', port=443): "
        f"Max retries exceeded with url: {url_with_query}"
    )


def _vk_transport_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise _transport_error(
            f"/method/users.get?access_token={VK_MARKER}&v=5.199"
        )

    monkeypatch.setattr(social_auth.requests, "get", _boom)


def _google_transport_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise _transport_error(f"/tokeninfo?id_token={GOOGLE_MARKER}")

    monkeypatch.setattr(social_auth.requests, "get", _boom)


def _text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


class TestVkTransportFailure:
    """`verify_vk_token` — сетевой сбой, ветка `RequestException`."""

    def test_the_token_is_not_logged(self, caplog, monkeypatch):
        _vk_transport_failure(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        captured = _text(caplog)
        assert captured, "перехват пуст — утверждения ниже ничего не проверяют"
        assert VK_MARKER not in captured

    def test_no_url_or_query_is_logged(self, caplog, monkeypatch):
        _vk_transport_failure(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        captured = _text(caplog)
        assert captured
        assert "access_token" not in captured
        assert "?" not in captured and "&" not in captured
        assert not _opaque_runs(captured), (
            "в логе прогон формы токена: длинный, с цифрой и заглавной"
        )

    def test_the_failure_class_is_named(self, caplog, monkeypatch):
        """Положительная стража: отказ обязан остаться диагностируемым.

        Без неё «убрать всё» прошло бы как починка, и следующий сбой сети
        стал бы неотличим от отсутствия сбоя.
        """
        _vk_transport_failure(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        assert "ConnectionError" in _text(caplog)


class TestGoogleTransportFailure:
    """`verify_google_token` — сетевой сбой, ветка `RequestException`."""

    def test_the_token_is_not_logged(self, caplog, monkeypatch):
        _google_transport_failure(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_google_token("unused-by-the-stub")
        captured = _text(caplog)
        assert captured, "перехват пуст — утверждения ниже ничего не проверяют"
        assert GOOGLE_MARKER not in captured

    def test_no_url_or_query_is_logged(self, caplog, monkeypatch):
        _google_transport_failure(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_google_token("unused-by-the-stub")
        captured = _text(caplog)
        assert captured
        assert "id_token" not in captured
        assert "?" not in captured and "&" not in captured
        assert not _opaque_runs(captured)

    def test_the_failure_class_is_named(self, caplog, monkeypatch):
        _google_transport_failure(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_google_token("unused-by-the-stub")
        assert "ConnectionError" in _text(caplog)


class TestVkJsonBranch:
    """Ветка `ValueError` у VK — ОБЪЯВЛЕНА ЗЕЛЁНОЙ до правки.

    Она ловится тем же `except`, но URL не несёт. Узел стоит здесь не
    ради починки, а чтобы зелень по нетекущей ветке нельзя было предъявить
    как доказательство: если однажды покраснеет — значит форма отказа
    изменилась и предмет надо пересматривать.
    """

    def test_the_value_error_branch_leaks_nothing(self, caplog, monkeypatch):
        class _NoJson:
            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        monkeypatch.setattr(
            social_auth.requests, "get", lambda *a, **k: _NoJson()
        )
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        captured = _text(caplog)
        assert captured
        assert VK_MARKER not in captured


class TestTheCapture:
    """Сторож пустого перехвата — ОБЪЯВЛЕН ЗЕЛЁНЫМ до правки."""

    def test_the_capture_is_not_empty(self, caplog, monkeypatch):
        _vk_transport_failure(monkeypatch)
        with _capturing(caplog):
            with pytest.raises(social_auth.SocialAuthTokenError):
                social_auth.verify_vk_token("unused-by-the-stub")
        assert _text(caplog).strip(), (
            "перехват пуст: логгер users.* объявлен propagate=False, "
            "и подвеска на корень записей не видит"
        )
