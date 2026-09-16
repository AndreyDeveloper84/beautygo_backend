"""Геокодирование МЕСТ построено и выключено — состояние обязано быть видно (DRF-2028).

Третье состояние
----------------

У обратного геокодирования сторож есть: ``geocoding.W001`` (DRF-1685) говорит,
что региональная цена по координатам всегда ``default``. У **прямого** пути —
геокодирования мест (`ServiceLocation`) — сторожа нет, хотя механизм тот же:
построен, полностью, и выключен. Команда ``geocode_locations`` отказывается
честно (`tenants/management/commands/geocode_locations.py:103-107`, «ни одного
запроса не сделано»), но **только когда её запустят**. Пока её никто не
запускает, состояние невидимо: ни одна строка при ``manage.py check`` о нём не
говорит.

Почему предикат — по РЕЕСТРУ, а не по ``GEOCODING_PROVIDER``
------------------------------------------------------------

``GEOCODING_PROVIDER`` объявлен (`djangoProject/settings/base.py:767`) для
**обратного** пути. Прямой берёт провайдера из обязательного аргумента
``--provider`` и этой настройки не читает вовсе. Сторож на ней утверждал бы
готовность того, чего прямой путь не касается, — верное утверждение не о том,
то есть ровно дефект, ради которого этот лист заведён.

Поэтому предикат спрашивает: **есть ли в реестре хоть один готовый
провайдер**. Это и решает, могут ли места когда-либо получить координаты,
какое бы имя оператор ни передал в прогоне.

Почему два предупреждения на одну причину — и это выбор, а не недосмотр
----------------------------------------------------------------------

При пустом ключе ``dadata`` заговорят обе проверки. Они **не** объединены и
**не** глушат друг друга:

* называют разные последствия: W001 — «цена всегда default»; W002 — «у мест не
  появятся координаты»;
* гаснут независимо: настроенный хост второго провайдера гасит W002, а W001
  продолжит говорить, пока ``GEOCODING_PROVIDER`` указывает на ненастроенного,
  — и это правда, а не шум;
* объединение воспроизвело бы исходный дефект: одно сообщение, описывающее
  соседний путь.

Узел ``test_the_neighbour_still_speaks`` держит это утверждение: он краснеет,
если W001 заглушить.

Чего этот файл НЕ проверяет
---------------------------

Сети здесь нет ни в одном узле: ``check()`` всех провайдеров обязан отвечать
до первого запроса (`core/geocoding/providers/base.py`), и проверка читает
только настройки.
"""
from __future__ import annotations

import pytest


def _run_places():
    from core.geocoding.checks import places_geocoding_is_not_a_fiction

    return places_geocoding_is_not_a_fiction(None)


def _run_reverse():
    from core.geocoding.checks import reverse_geocoding_is_not_a_fiction

    return reverse_geocoding_is_not_a_fiction(None)


@pytest.fixture
def unconfigured(settings):
    """Состояние пилота на 16.09: ни одна настройка геокодера не задана.

    Замер по репозиторию: в `.env.example`, `.env.prod.example`,
    `docker-compose.yml` и `docker-compose.dev.yml` — ноль упоминаний
    настроек геокодера при ненулевом положительном контроле на каждом файле.
    """
    settings.DADATA_API_KEY = ""
    settings.NOMINATIM_BASE_URL = ""
    settings.GEOCODING_PROVIDER = "dadata"
    settings.GEOCODING_REQUIRE_LIVE_REVERSE = False
    return settings


class TestPlacesGeocodingReadiness:
    def test_unconfigured_registry_is_named_as_a_warning(self, unconfigured):
        """Ровно один узел и ровно СВОЙ идентификатор.

        Сверка по списку идентификаторов, а не по «есть хоть одно
        предупреждение»: `manage.py check` не молчит и без этой проверки —
        W001 говорит своё, — и узел «что-то предупредило» был бы зелен по
        чужой причине.
        """
        assert [m.id for m in _run_places()] == ["geocoding.W002"]

    def test_the_message_is_about_places_not_about_pricing(self, unconfigured):
        message = _run_places()[0].msg

        assert "мест" in message, message
        assert "координат" in message, message
        assert "региональная цена" not in message, "это текст W001, а не свой"
        assert "default" not in message, "это текст W001, а не свой"

    def test_the_message_names_what_to_fill(self, unconfigured):
        """Причину неготовности даёт сам провайдер — значит в тексте видно,
        чего именно не хватает, а не «что-то не настроено»."""
        message = _run_places()[0].msg

        assert "DADATA_API_KEY" in message or "NOMINATIM_BASE_URL" in message, message

    def test_the_hint_leads_to_the_registry(self, unconfigured):
        """Подсказка обязана быть исполнимой: человек должен узнать, ИЗ ЧЕГО
        выбирать. Адрес реестра — модуль и команда, печатающая имена."""
        hint = _run_places()[0].hint or ""

        assert "core/geocoding/providers" in hint, hint
        assert "geocode_locations" in hint, hint

    def test_a_configured_provider_silences_it(self, unconfigured):
        """Достаточно одного готового провайдера: места смогут получить
        координаты, каким бы именем ни запустили прогон."""
        unconfigured.NOMINATIM_BASE_URL = "https://geo.example.invalid"

        assert _run_places() == []


class TestTheNeighbourIsNotSilenced:
    def test_the_neighbour_still_speaks(self, unconfigured):
        """W001 продолжает говорить своё — это утверждение, а не надежда.

        Узел зелен и до правки: он держит от соблазна «убрать дубль»,
        заглушив соседа. Проба на него объявлена отдельно — заглушить W001,
        и покраснеть обязан ровно он.
        """
        messages = _run_reverse()

        assert [m.id for m in messages] == ["geocoding.W001"]
        assert "default" in messages[0].msg


def test_both_checks_are_wired_into_manage_py_check():
    """Проверки зарегистрированы ЗАГРУЗКОЙ приложения, а не импортом из теста.

    Идиома скопирована из ``services/tests/test_regional_pricing.py`` (узел
    ``test_the_check_is_wired_into_manage_py_check``): там первая редакция
    импортировала модуль проверок сама и тем самым сама её регистрировала —
    подмена, выкинувшая импорт из ``apps.py``, оставалась зелёной. Если
    чинить одну из двух, вторую искать здесь.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = dict(
        os.environ,
        DADATA_API_KEY="",
        NOMINATIM_BASE_URL="",
        GEOCODING_PROVIDER="dadata",
        GEOCODING_REQUIRE_LIVE_REVERSE="false",
    )
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, "manage.py", "check"],
        cwd=root, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    combined = out.stdout + out.stderr

    assert "geocoding.W002" in combined, combined[-800:]
    assert "geocoding.W001" in combined, combined[-800:]
