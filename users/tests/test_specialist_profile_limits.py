"""Ответ профиля мастера несёт лимиты — один источник для экрана 07 (M22).

Решение главного окна 15.09 (план M22, вопрос 2): Mini App не держит свои
числа («О себе» 280 против 500 в каталоге, фото 10 МБ против 5 МБ). Каталог
отдаёт в состоянии профиля ``limits`` — те же константы, по которым
проверяет запись:

* ``bio`` — ``BIO_MAX_LENGTH``;
* ``display_name_min`` — ``DISPLAY_NAME_MIN_LENGTH``;
* ``avatar_bytes`` — ``AVATAR_MAX_BYTES``;
* ``portfolio_bytes`` — ``PORTFOLIO_MAX_BYTES``;
* ``portfolio_count`` — ``PORTFOLIO_LIMIT``.

Что стережётся: лимиты есть в каждом ответе, отдающем состояние профиля
(GET/PATCH профиля, загрузка и удаление аватара), и каждый объявленный лимит
равен границе, которую проверка реально держит — на самом значении и на
значении +1. Иначе число в ответе стало бы вторым описанием правила, которое
разойдётся с первым молча.

Красный до правки: весь файл (``limits`` в ответе нет).
"""

from __future__ import annotations

import pytest

from users.tests.test_specialist_profile_m21_1813 import (
    OLGA,
    _avatar_url,
    _client,
    _image,
    _portfolio_url,
    _profile_url,
    _upload,
)
from users.tests.test_specialist_profile_m21_1813 import _settings as _m21_settings  # noqa: F401
from users.tests.test_specialist_profile_m21_1813 import olga as _m21_olga

pytestmark = pytest.mark.django_db

# Фикстуры модуля M21 — присваиванием: pytest находит их по атрибутам модуля.
_settings = _m21_settings
olga = _m21_olga

EXPECTED_KEYS = {"bio", "display_name_min", "avatar_bytes", "portfolio_bytes", "portfolio_count"}


def _limits(response) -> dict:
    assert response.status_code in (200, 201), response.content
    limits = response.json()["data"].get("limits")
    assert isinstance(limits, dict), f"в ответе нет limits: {response.json()['data']}"
    assert set(limits) == EXPECTED_KEYS, limits
    return limits


class TestEveryProfileStateCarriesTheLimits:
    def test_get_profile(self, olga):
        limits = _limits(_client().get(_profile_url(olga)))

        assert limits == {
            "bio": 500,
            "display_name_min": 2,
            "avatar_bytes": 5 * 1024 * 1024,
            "portfolio_bytes": 10 * 1024 * 1024,
            "portfolio_count": 10,
        }

    def test_patch_profile(self, olga):
        r = _client().patch(_profile_url(olga), {"bio": "Опыт"}, format="json")

        assert _limits(r)["bio"] == 500

    def test_avatar_upload(self, olga):
        r = _upload(_avatar_url(olga), _image())

        assert _limits(r)["avatar_bytes"] == 5 * 1024 * 1024

    def test_avatar_delete(self, olga, django_capture_on_commit_callbacks):
        _upload(_avatar_url(olga), _image())
        with django_capture_on_commit_callbacks(execute=True):
            r = _client().delete(_avatar_url(olga))

        assert _limits(r)["portfolio_count"] == 10


class TestTheDeclaredLimitIsTheEnforcedBoundary:
    """Число в ответе проверяется поведением записи, а не сравнением с
    константой модуля: на значении — принято, на значении +1 — отказ."""

    def test_bio(self, olga):
        limit = _limits(_client().get(_profile_url(olga)))["bio"]

        ok = _client().patch(_profile_url(olga), {"bio": "б" * limit}, format="json")
        too_long = _client().patch(_profile_url(olga), {"bio": "б" * (limit + 1)}, format="json")

        assert ok.status_code == 200, ok.content
        assert too_long.status_code == 400, too_long.content

    def test_display_name_min(self, olga):
        minimum = _limits(_client().get(_profile_url(olga)))["display_name_min"]

        ok = _client().patch(_profile_url(olga), {"display_name": "А" * minimum}, format="json")
        short = _client().patch(
            _profile_url(olga), {"display_name": "А" * (minimum - 1)}, format="json"
        )

        assert ok.status_code == 200, ok.content
        assert short.status_code == 400, short.content

    def test_portfolio_count(self, olga):
        limit = _limits(_client().get(_profile_url(olga)))["portfolio_count"]

        accepted = [_upload(_portfolio_url(olga), _image()).status_code for _ in range(limit)]
        extra = _upload(_portfolio_url(olga), _image())

        assert accepted == [201] * limit
        assert extra.status_code == 400, extra.content
        assert extra.json()["error"]["details"]["reason"] == "portfolio_limit_exceeded"

    def test_avatar_bytes(self, olga):
        limit = _limits(_client().get(_profile_url(olga)))["avatar_bytes"]

        too_big = _upload(_avatar_url(olga), _image(pad_to=limit + 1))

        assert too_big.status_code == 400, too_big.content
        details = too_big.json()["error"]["details"]
        assert (details["reason"], details["limit_bytes"]) == ("file_too_large", limit)

    def test_portfolio_bytes(self, olga):
        limit = _limits(_client().get(_profile_url(olga)))["portfolio_bytes"]

        too_big = _upload(_portfolio_url(olga), _image(pad_to=limit + 1))

        assert too_big.status_code == 400, too_big.content
        details = too_big.json()["error"]["details"]
        assert (details["reason"], details["limit_bytes"]) == ("file_too_large", limit)


_ = OLGA
