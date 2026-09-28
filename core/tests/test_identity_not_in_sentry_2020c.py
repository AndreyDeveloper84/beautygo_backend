"""Внешняя личность не уезжает в Sentry — ни одним носителем (DRF-2020 C).

Замер, с которого начался лист: идентификатор попадает в ТЕКСТ ИСКЛЮЧЕНИЯ, а
чистка политики (`core.sentry_policy.scrub_event`) текст исключения не трогает.
Это трансграничная передача персональных данных, а не шумный лог.

Перепись, сделанная прогоном (маркер в каждое поле → настоящая чистка):

* носителей с маркером до чистки — 23, **после — 13**. То есть «починить
  `exception[].value`» оставило бы двенадцать: `transaction`, `message`,
  `logentry.params`/`formatted`, `breadcrumbs[].message`,
  `breadcrumbs[].data.*` (чистился ТОЛЬКО ключ `url`), `extra.*`,
  `contexts.*.data.*`, `tags.*`, `spans[].description` (события
  производительности идут через ту же чистку), `fingerprint[]`, `server_name`;
* мест, где форма печатается вместо значения, — **11**: девять в
  `users/services.py`, по одному в `users/account_reset.py` и
  `users/identity_card.py` (пересчитано после ревью: прежняя цифра 13 считала
  ещё две строки доктеста, которые никто не исполняет);
* достижимость: `InvalidExternalUserIDError` упомянут в восьми не-тестовых
  файлах и перехвачен в **семи**; один не перехватывает
  (`appointments/management/commands/bootstrap_e2e_wave1.py`), то есть путь до
  необработанного 500 и до отправки существует. Прежняя формулировка
  «четыре / четыре» не воспроизводится ни при одном определении области —
  снята.

Что НЕ покрыто первым слоем и держится только вторым (найдено ревью):
`users/account_reset.py` `NotAllowed.__init__` собирает текст внутри САМОГО
класса исключения, поэтому обход мест `raise` его не видит; а несёт он
`listed_as` — операторскую форму `<канал>:<id>`. Это двенадцатый носитель, и он
лучший аргумент за вторую линию, какой у нас есть.

Отдельная находка того же замера: к событию Sentry в каталоге **вообще не
применялась** редактура персональных данных. `core.pii_log_filter.redact_pii`
(телефон, почта, карта) стояла на логах и на операторской строке алертов, но не
на событии — значит телефон в тексте исключения уходил наружу так же, как
идентификатор.

Починка в два слоя:

1. **не класть значение в текст** — в тех 11 местах печатается ФОРМА (источник и
   длина), а не значение. Проверяется НЕ здесь, а в
   `users/tests/test_external_id_shape_2020c.py`: ревью подменой показало, что
   первый слой можно было удалить целиком, и все узлы этого файла оставались
   зелёными;
2. **чистить событие** — текстовые листья события проходят через `redact_pii`,
   одно определение на логи, алерты и Sentry. Вторая линия нужна потому, что
   первую нарушит следующий разработчик, и молча. Проверяется здесь.

Узлы идут ЧЕРЕЗ НАСТОЯЩИЙ SDK и WSGI-вход каталога (как
`test_sentry_policy_live_sdk`), а не зовут `scrub_event` напрямую: у нас уже
было, что двадцать один зелёный узел был правдой про функцию и неправдой про
продукт — поле не переносилось через шов, и узлы этого не видели.

И рядом с «идентификатора нет» стоит «событие дошло и несёт то, что должно»:
первое утверждение верно и о пустом событии, то есть зелёный сторож над
сломанной отправкой выглядел бы так же.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.transport import Transport

from core.sentry_policy import init_options

#: Внешняя личность той же формы, что ходит в `X-External-User-ID`
#: (`<source>:<id>`). Значение синтетическое, но форма настоящая — именно её
#: подставляли в текст исключения.
IDENTITY = "max:729481"

#: Телефон в тестовом диапазоне (сторож PII разрешает 900/999).
PHONE = "+79991234567"

PATH = "/api/v1/sentry-probe/identity/boom/"
REQUEST_ID = "req-live-identity-2020c"


class _CaptureTransport(Transport):
    """Конверты — в список; в сеть ничего не уходит."""

    def __init__(self, options=None):
        super().__init__(options)
        self.envelopes: list = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def flush(self, timeout=None, callback=None):
        return None

    def kill(self):
        return None


@pytest.fixture
def live_sentry():
    previous = sentry_sdk.get_client()
    transport = _CaptureTransport()
    sentry_sdk.init(
        **init_options(
            dsn="http://public@127.0.0.1/1",
            environment="test",
            release=None,
            traces_sampler=lambda context: 1.0,
        ),
        integrations=[DjangoIntegration()],
        transport=transport,
    )
    try:
        yield transport
    finally:
        sentry_sdk.get_global_scope().set_client(previous)


def _call_wsgi(app, *, path, query="", body=b"", headers=None):
    environ = {
        "REQUEST_METHOD": "POST",
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "203.0.113.77",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        **(headers or {}),
    }
    status: list = []
    response = app(environ, lambda s, h, exc_info=None: status.append(s))
    try:
        b"".join(response)
    finally:
        getattr(response, "close", lambda: None)()
    return status[0]


def _error_events(transport) -> list[dict]:
    events = []
    for envelope in transport.envelopes:
        for item in envelope.items:
            if item.headers.get("type") == "event":
                events.append(json.loads(bytes(item.payload.get_bytes())))
    return events


def _carriers_with(payload, needle: str, path: str = "") -> list[str]:
    """Пути всех носителей, где встречается строка. Путь, а не факт: сообщение
    сторожа должно НАЗЫВАТЬ поле, иначе разбор начнётся с нуля."""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found += _carriers_with(value, needle, f"{path}.{key}" if path else str(key))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found += _carriers_with(value, needle, f"{path}[{index}]")
    elif isinstance(payload, str) and needle in payload:
        found.append(path or "<корень>")
    return found


@pytest.mark.django_db
@pytest.mark.urls("core.tests.sentry_live_probe_urls")
class TestTheIdentityDoesNotLeaveInAnyCarrier:
    def _run(self, live_sentry) -> dict:
        from djangoProject.wsgi import application

        _call_wsgi(
            application,
            path=PATH,
            query="page=2",
            body=json.dumps({"note": "ok"}).encode(),
            headers={
                "HTTP_X_REQUEST_ID": REQUEST_ID,
                "HTTP_X_APP_TYPE": "client",
                "HTTP_X_EXTERNAL_USER_ID": IDENTITY,
            },
        )
        events = _error_events(live_sentry)
        assert events, "SDK не отправил события — проверять нечего"
        return events[0]

    def test_the_identity_is_in_no_carrier_of_the_real_event(self, live_sentry):
        """Отсутствие И место замены — в ДВУХ носителях, а не в одном.

        «Личности нет» выполнимо и пустым событием, и событием, где вырезано не
        то. Поэтому рядом стоит утверждение, что след замены лежит ИМЕННО там,
        откуда личность убрали.

        И носителей два, потому что с одним узел был слабее, чем выглядел
        (найдено ревью): пробник кладёт личность только в текст исключения, и
        подмена «чистить ТОЛЬКО `exception`» проходила зелёной — та самая узкая
        починка, против которой написана рекурсия. Второй носитель
        (`extra.probe_identity_note`) структурно другой, и теперь эта подмена
        краснеет.

        Замер, который стоит помнить: третье место — хлебная крошка из журнала —
        приходит в событие УЖЕ чистой, её правит фильтр ПДн на самой записи.
        Крошкой рекурсию доказать нельзя, поэтому она проверяется отдельно и как
        утверждение о нижнем слое.
        """
        event = self._run(live_sentry)

        carriers = _carriers_with(event, IDENTITY)

        assert carriers == [], (
            "внешняя личность уехала бы в Sentry; носители: " + ", ".join(carriers)
        )
        exception_text = (event["exception"]["values"][-1] or {}).get("value") or ""
        assert "[IDENTITY]" in exception_text, (
            "в тексте исключения нет следа замены — значит вырезали не там, "
            f"а личности нет по другой причине: {exception_text[:80]!r}"
        )
        note = (event.get("extra") or {}).get("probe_identity_note") or ""
        assert "[IDENTITY]" in note, (
            "в `extra` нет следа замены — значит чистится только текст "
            f"исключения, а остальные двенадцать носителей нет: {note!r}"
        )

    def test_the_breadcrumb_arrives_clean_by_either_layer(self, live_sentry):
        """Крошка доезжает чистой — ЛЮБЫМ из двух слоёв, и узел не различает их.

        История этого докстринга стоит того, чтобы её сохранить, потому что я
        соврал в нём дважды.

        Сперва я написал, что крошка приходит в событие НЕтронутой (фильтр ПДн
        стоит на обработчике `console`, а крошки Sentry собирает своим). Замер
        опроверг: фильтр правит саму запись журнала, и Sentry видит её после
        фильтра.

        Тогда я написал, что узел закрепляет НИЖНИЙ слой и что «ни один другой
        узел этого не увидит». Ревью опровергло подменой оба утверждения:
        убитый фильтр журнала (`_redact_record` → `return`) оставляет узел
        ЗЕЛЁНЫМ, потому что `breadcrumbs[].message` — обычный строковый лист, и
        рекурсия чистит его сама; а нижний слой при этом роняет четыре узла в
        `core/tests/test_log_pii_2272.py`.

        Что узел держит на самом деле: крошка из журнала доезжает до события
        чистой. Каким слоем — он не знает, и знать не может: рекурсия всегда
        ответит за фильтр. Настоящее утверждение о нижнем слое на этом шве
        требует смотреть на крошку ДО `before_send` (хук `before_breadcrumb`) —
        это не сделано, и потому здесь не обещано.
        """
        event = self._run(live_sentry)

        crumbs = ((event.get("breadcrumbs") or {}).get("values")) or []
        mine = [c for c in crumbs if "probe resolving" in str(c.get("message") or "")]
        assert mine, f"крошки пробника нет — проверять нечего: {len(crumbs)} крошек"
        assert "[IDENTITY]" in mine[0]["message"], mine[0]["message"]

    def test_a_phone_in_the_same_text_is_also_gone(self, live_sentry):
        """Тот же замер показал, что редактура ПДн к событию не применялась
        вовсе: телефон в тексте исключения уходил наружу наравне с
        идентификатором."""
        event = self._run(live_sentry)

        carriers = _carriers_with(event, PHONE)

        assert carriers == [], "телефон уехал бы в Sentry; носители: " + ", ".join(carriers)

    def test_the_event_still_carries_what_it_must(self, live_sentry):
        """Положительная сторона: «личности нет» правда и о пустом событии.

        Поэтому рядом стоит утверждение, что событие ДОШЛО и несёт диагностику:
        класс ошибки, маршрут и correlation id. Без этого зелёный сторож над
        сломанной отправкой выглядел бы точно так же.
        """
        event = self._run(live_sentry)

        values = (event.get("exception") or {}).get("values") or []
        assert values, f"в событии нет исключения: {sorted(event)}"
        assert values[-1]["type"] == "RuntimeError"
        assert "sentry-probe" in str(event.get("transaction") or ""), event.get("transaction")
        assert (event.get("tags") or {}).get("request_id") == REQUEST_ID
        # Диагностика самого падения: место в коде осталось на месте, то есть
        # редактура не съела то, по чему инцидент ищут.
        frames = (values[-1].get("stacktrace") or {}).get("frames") or []
        assert frames, "у исключения нет кадров стека — искать инцидент нечем"
        assert any("sentry_live_probe_urls" in str(f.get("filename") or "") for f in frames)


#: Имена, за которыми в этом репозитории стоит внешний идентификатор.
_IDENTITY_SLOTS = frozenset({
    "external_user_id",
    "HTTP_X_EXTERNAL_USER_ID",
    "X-External-User-ID",
    "external_id",
})

#: `.claude` — вложенные рабочие деревья: без этого сканер видит 28 копий
#: репозитория и краснеет на машине автора, оставаясь зелёным в CI.
_SKIP_DIRS = frozenset({"venv", ".venv", "node_modules", "__pycache__", ".claude", ".git"})

#: Форма контракта (`users.services._EXTERNAL_USER_ID_RE`), но НЕ импорт его:
#: узел должен краснеть и если контракт ослабят, а не подстраиваться под него.
_CONTRACT_SHAPE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9_-]{1,64})+$")


@lru_cache(maxsize=1)
def _identity_fixtures() -> tuple[tuple[str, str], ...]:
    """{значение: где впервые встретилось} — по разбору кода, не по грепу.

    Кэш и обход с ОТСЕЧЕНИЕМ каталогов, а не `rglob` с фильтром после: узлов,
    зовущих перепись, два, и без кэша дерево разбиралось дважды — замер дал
    8.9 с и 5.9 с. С одним разбором и отсечением — один раз и быстрее, потому
    что `rglob` сначала перечисляет всё (включая вложенные рабочие деревья) и
    только потом отбрасывает.
    """
    repo = Path(__file__).resolve().parents[2]
    found: dict[str, str] = {}

    def note(value: object, rel: str, lineno: int) -> None:
        if isinstance(value, str) and _CONTRACT_SHAPE.match(value):
            found.setdefault(value, f"{rel}:{lineno}")

    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(repo).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except (SyntaxError, ValueError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg in _IDENTITY_SLOTS and isinstance(kw.value, ast.Constant):
                            note(kw.value.value, rel, kw.value.lineno)
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value in _IDENTITY_SLOTS
                            and isinstance(value, ast.Constant)
                        ):
                            note(value.value, rel, value.lineno)
    return tuple(found.items())


class TestEveryIdentityTheCatalogCarries:
    """Шаблон проверяется ВОКАБУЛЯРОМ ПРОДУКТА, а не соседним шаблоном.

    Прежний узел сравнивал `_IDENTITY_RE` с `_HAS_PII_CANDIDATE` — то есть две
    регулярки друг с другом. Оба построены из одного `_IDENTITY_SOURCES`,
    поэтому названная им беда («списки разойдутся») стала невозможной по
    построению, а настоящая беда — «из списка убрали источник, который каталог
    носит» — его не роняла: `_IDENTITY_SOURCES = "bot"` проходило зелёным, пока
    `max:729481` уезжал наружу (найдено ревью).

    Опора здесь другая: значения, которые САМ каталог ставит на место внешнего
    идентификатора — именованный аргумент/ключ `external_user_id`, заголовок
    `X-External-User-ID`. Замер по дереву даёт **66** различных значений с
    единственным источником `bot`, и из них **30** операторских форм с
    источниками `max` и `telegram`. Уберут источник из списка — узел краснеет и
    называет значение.

    (Число исправлено после ревью: стояло 100 — цифра другого, более широкого
    сканера, который брал ещё и присваивания переменным. Узел считает 66, и в
    листе, чья тема — воспроизводимые числа, докстринг обязан описывать ТОТ
    обход, который исполняется.)

    Пределы, и их два:

    * узел видит только ЛИТЕРАЛЫ в именованных позициях. Значение, прочитанное
      прямо из запроса, ему не видно — так и вышло с
      `users/internal_identity_api.py`, где идентификатор берётся из
      `request.META.get(...)`: мой же сканер недосчитал это место, а ревью
      назвало;
    * канал, которого в дереве НЕТ, узел не потребует. Контракт заголовка
      принимает любой источник, поэтому полноту этот узел дать не может — её
      даст только общий список каналов для резолвера и редактуры (отдельный
      лист, причина в комментарии у `_IDENTITY_SOURCES`).
    """

    def _fixtures(self) -> dict[str, str]:
        return dict(_identity_fixtures())

    @staticmethod
    def _operator_spec(value: str) -> str | None:
        """`bot:max:123` → `max:123` — форма, которую каталог показывает оператору.

        Не выдумка узла: ровно это делает `users.account_reset._spec_of`
        («bot:max:123 → max:123»), и именно она лежит в `NotAllowed.listed_as`,
        то есть в тексте исключения. Поэтому список источников обязан покрывать
        и её, а не только обёртку `bot:`.
        """
        parts = value.split(":")
        return ":".join(parts[1:]) if len(parts) >= 3 else None

    def test_the_census_is_not_vacuous(self):
        """Нижняя граница: сканер, посмотревший не туда, не должен пройти на
        пустом результате. И оба источника обязаны быть настоящими."""
        fixtures = self._fixtures()
        specs = {self._operator_spec(v) for v in fixtures} - {None}

        assert len(fixtures) >= 50, f"перепись нашла только {len(fixtures)} — не тот корень?"
        assert {v.split(":")[0] for v in fixtures} == {"bot"}, sorted(fixtures)[:5]
        assert {s.split(":")[0] for s in specs} == {"max", "telegram"}, sorted(specs)[:5]

    def test_a_long_identity_is_redacted_whole_not_by_its_head(self):
        """Сегментов в контракте НЕ ТРИ, а сколько угодно.

        `users.services._EXTERNAL_USER_ID_RE` — `(?::[A-Za-z0-9_-]{1,64})+`, без
        верхней границы. Пока шаблон редактуры стоял на `{1,3}`,
        `bot:max:a:b:c` превращался в `[IDENTITY]:c`: хвост личности оставался и
        выглядел как чистый текст — хуже, чем нетронутая строка, потому что
        читается как «здесь уже почистили».

        Узел заведён отдельно, потому что подмена `+` → `{1,3}` не уронила
        НИЧЕГО: все 100 значений переписи короче четырёх сегментов, и граница
        была свободна.
        """
        import re as _re

        from core.pii_log_filter import redact_pii
        from users.services import _EXTERNAL_USER_ID_RE

        long_identity = "bot:max:region-7:device-2:729481"
        assert _EXTERNAL_USER_ID_RE.match(long_identity), "образец не по контракту"

        cleaned = redact_pii(f"resolve failed for {long_identity} on this request")

        assert "[IDENTITY]" in cleaned, cleaned
        # Ни одного куска исходного значения: хвост — это тоже личность.
        leftovers = [
            part for part in long_identity.split(":") if _re.search(rf"\b{part}\b", cleaned)
        ]
        assert leftovers == [], f"от личности остался хвост {leftovers}: {cleaned!r}"

    def test_redaction_removes_every_identity_the_catalog_uses(self):
        from core.pii_log_filter import redact_pii

        wanted: dict[str, str] = {}
        for value, where in self._fixtures().items():
            wanted[value] = where
            spec = self._operator_spec(value)
            if spec:
                wanted[spec] = f"{where} (форма для оператора)"

        survived = {
            value: where
            for value, where in wanted.items()
            if value in redact_pii(f"resolve failed for {value} on this request")
        }

        assert survived == {}, (
            "эти значения каталог считает внешней личностью, а редактура их не "
            f"убирает — уйдут в Sentry и в журнал: {survived}"
        )


class TestThePrefilterDoesNotSwallowTheIdentity:
    """Дешёвый префильтр стоит ПЕРЕД редактурой и решает, звать ли её вообще.

    Значит он — не оптимизация, а часть охраны: текст, который он отсёк,
    редактуру не проходит, и личность уходит наружу молча. Отказ был бы
    невидимым — ни исключения, ни записи в журнале, просто чистая строка,
    которую никто не чистил.

    Регистр проверяется ОБОИМИ: префильтр был регистронезависимым, а шаблон
    личности — нет, и `MAX:729481` проходил префильтр, но не редактуру. Это та
    же форма отказа: пустил и не почистил.

    Чего этот узел НЕ доказывает (и потому рядом стоит
    `TestEveryIdentityTheCatalogCarries`): он сравнивает два шаблона друг с
    другом, поэтому «из списка убрали настоящий источник» ему не видно.
    """

    def test_the_prefilter_lets_through_everything_the_patterns_catch(self):
        from core.pii_log_filter import (
            _HAS_PII_CANDIDATE,
            _IDENTITY_RE,
            _IDENTITY_SOURCES,
            redact_pii,
        )

        sources = _IDENTITY_SOURCES.split("|")
        # `assert sources` не годится: `"".split("|") == [""]`, а это истина.
        # Пустой источник дал бы шаблон, который ловит любое `:слово` — и узел
        # об этом молчал бы (найдено ревью).
        assert all(sources), f"в списке источников есть пустой: {sources!r}"

        missed = []
        for source in sources + [s.upper() for s in sources]:
            # БЕЗ цифр и без «@» — намеренно. С `a1b2c3` узел был зелёным и на
            # подмене: префильтр срабатывал на цифрах образца, а не на списке
            # источников, то есть проверялось что угодно, кроме проверяемого
            # утверждения (поймано подменой, а не чтением).
            sample = f"upstream said {source}:abcdef is unknown"
            # Премиссу утверждаем первой: если шаблон личности сам не видит
            # образец, узел проверял бы префильтр на том, что чистить не надо.
            assert _IDENTITY_RE.search(sample), source
            if not _HAS_PII_CANDIDATE.search(sample):
                missed.append(source)
            elif "[IDENTITY]" not in redact_pii(sample):
                missed.append(f"{source} (префильтр пустил, редактура не сработала)")

        assert missed == [], (
            "префильтр отсекает текст, который шаблон личности обязан почистить — "
            f"наружу уйдёт молча: {missed}"
        )
