"""Внешний идентификатор и телефон не попадают в логи каталога.

Предмет — класс A переписи DRF-2020: личность в **аргументах** ``logger.*``.
На `origin/dev` таких вызовов 16 в 6 файлах; после #488 остаётся **13 в 5**
(``users/sms.py`` уходит целиком). Здесь закрываются эти 13.

**Что ставится вместо личности.** Неидентифицирующая зацепка, **уже
существующая в данных** — внутренний первичный ключ, — а не новый
идентификатор ради лога и не хеш от снимаемого значения.

**Псевдоним против анонимизации — проверено, а не предположено.**
``User.id`` это ``UUIDField(default=uuid.uuid4)``, от ``external_user_id`` не
производен. Но прокси-строка **ищется по** ``username=external_user_id``
(``users/services.py:139,214,398,587,663``). Значит ``user.pk`` — анонимизация,
а ``user.username`` был бы **тем же самым значением под другим именем**.
Перепись ниже запрещает и его, чтобы следующий не «упростил» на username.

**Где зацепки нет по построению.** ``users/identity_events.py`` принимает
``actor``, и докстринг говорит прямо: это целевой пользователь, **когда он
существует**; вызывающие передают ``actor=None``
(``users/services.py:368,385,650``) для отказов, чей субъект так и не
разрешился. На этих случаях адресата нет — событие пишется без него, и это
названо, а не залатано выдуманным идентификатором.

**Две ловушки перехвата, обе реальны в этом дереве.**
1. Логгер ``users`` объявлен ``propagate=False`` (``settings/base.py``,
   ``LOGGING``), поэтому корневой обработчик ``caplog`` записей ``users.*``
   **не видит**. ``nutrition`` своей записи в ``loggers`` не имеет и наверх
   проходит — то есть один и тот же приём дал бы разный результат в двух
   половинах предмета.
2. ``users/identity_events.py`` берёт логгер **явным именем**
   ``"users.identity.events"``, а не ``__name__``. Подвеска к
   ``users.identity_events`` слушала бы логгер, в который никто не пишет.
Поэтому ниже перехват всегда вешается на **named** логгер, и каждый узел
отдельно утверждает, что перехват не пуст.

**Предел, названный сознательно.** Семь мест в ``nutrition/views.py``
(``:312`` и пять ветвей целей) поведенческими узлами здесь не закрыты:
их запуск потребовал бы загрузки файла, подмены провайдеров и постройки
профилей в нужных состояниях — то есть проверял бы предметную область
питания, а не свойство логов. Все семь держит перепись по AST: она
краснеет, если идентификатор туда вернётся.
"""

from __future__ import annotations

import ast
import logging
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from analytics.models import AnalyticsEvent
from users.models import OTPCode, User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "svc-token-identity-logs"  # pragma: allowlist secret
PROVISIONING_TOKEN = "test-provisioning-identity-logs"  # pragma: allowlist secret
EXT_ID = "bot:max:9990001"
PHONE = "+79990000042"  # тестовый диапазон 999, как в users/tests/conftest.py

URL_PROFILE = "/api/v1/nutrition/internal/profile/"
URL_BIND = "/api/v1/internal/users/bind-external/"

FULL_INPUTS = {
    "gender": "female", "age": 36, "height_cm": 170, "weight_kg": 67.0,
    "activity_coefficient": 1.4, "goal": "lose",
}

#: Модули предмета и имена их логгеров. Имя берётся из кода, а не из пути:
#: identity_events объявляет логгер явно.
MODULES = {
    "nutrition/views.py": "nutrition.views",
    "users/identity_events.py": "users.identity.events",
    "users/internal_users_api.py": "users.internal_users_api",
    "users/services.py": "users.services",
    "users/social_auth.py": "users.social_auth",
}


@contextmanager
def _capturing(name: str, caplog):
    """Перехват на ИМЕНОВАННЫЙ логгер: ``users`` стоит ``propagate=False``,
    корневой обработчик caplog его записей не получает."""
    target = logging.getLogger(name)
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=name):
            yield
    finally:
        target.removeHandler(caplog.handler)


def _text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


@pytest.fixture
def real_user(db):
    return User.objects.create(username="real-identity-logs", role="client")


class TestTheProxyCreation:
    """``users/services.py`` — ленивое заведение прокси по внешнему идентификатору.

    Строка лога стоит внутри ``if created:`` в
    ``resolve_external_user`` (``users/services.py:106``), то есть срабатывает
    **только на первом** появлении идентификатора. Поэтому здесь свой,
    нигде больше не используемый идентификатор: возьми общий ``EXT_ID`` —
    строку создаст соседний класс, ``get_or_create`` найдёт готовую, ветка не
    исполнится, и узел молча проверит пустой перехват.
    """

    FRESH_ID = "bot:max:9990002"

    def test_the_external_id_is_not_logged(self, caplog):
        from users.services import resolve_external_user

        with _capturing("users.services", caplog):
            resolve_external_user(self.FRESH_ID)

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert self.FRESH_ID not in captured

    def test_the_internal_pk_is_still_logged(self, caplog):
        """Положительная стража: зацепка остаётся, иначе событие без адресата."""
        from users.services import resolve_external_user

        with _capturing("users.services", caplog):
            user = resolve_external_user(self.FRESH_ID)

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert str(user.pk) in captured


class TestTheBindExternalRoute:
    """``users/internal_users_api.py`` — ручка связывания личности."""

    @pytest.fixture
    def api(self, settings):
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING_TOKEN
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {PROVISIONING_TOKEN}")
        return client

    def test_the_external_id_is_not_logged(self, caplog, api, real_user):
        with _capturing("users.internal_users_api", caplog):
            response = api.post(
                URL_BIND,
                {"external_user_id": EXT_ID, "ayla_user_id": str(real_user.pk)},
                format="json",
            )
        assert response.status_code == 200, response.content

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert EXT_ID not in captured

    def test_the_internal_ids_are_still_logged(self, caplog, api, real_user):
        with _capturing("users.internal_users_api", caplog):
            api.post(
                URL_BIND,
                {"external_user_id": EXT_ID, "ayla_user_id": str(real_user.pk)},
                format="json",
            )

        captured = _text(caplog)
        assert "ayla_user_id=" in captured
        assert "request_id=" in captured


class TestTheIdentityAudit:
    """``users/identity_events.py`` — журнал связывания.

    Ветви отказа достигаются так же, как в
    ``users/tests/test_identity_binding_hardening.py``: подменой стока
    ``AnalyticsEvent.objects.create`` на исключение.
    """

    def test_the_external_id_is_not_logged_on_sink_failure(
        self, caplog, monkeypatch, real_user,
    ):
        from users.identity_events import emit_identity_binding

        def boom(*args, **kwargs):
            raise RuntimeError("sink is down")

        monkeypatch.setattr(AnalyticsEvent.objects, "create", boom)
        with _capturing("users.identity.events", caplog):
            emit_identity_binding(
                actor=real_user, external_user_id=EXT_ID,
                result="created", reason="ok", initiator="test",
            )

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert EXT_ID not in captured

    def test_the_actor_pk_is_logged_when_there_is_one(
        self, caplog, monkeypatch, real_user,
    ):
        from users.identity_events import emit_identity_binding

        def boom(*args, **kwargs):
            raise RuntimeError("sink is down")

        monkeypatch.setattr(AnalyticsEvent.objects, "create", boom)
        with _capturing("users.identity.events", caplog):
            emit_identity_binding(
                actor=real_user, external_user_id=EXT_ID,
                result="created", reason="ok", initiator="test",
            )

        assert str(real_user.pk) in _text(caplog)

    def test_without_an_actor_the_event_is_recorded_without_an_addressee(
        self, caplog, monkeypatch,
    ):
        """Зацепки нет по построению — событие пишется, адресата в нём нет.

        Вызывающие передают ``actor=None`` для отказов, чей субъект так и не
        разрешился (``users/services.py:368,385,650``). Выдумывать здесь
        идентификатор нельзя; можно только не потерять сам факт.
        """
        from users.identity_events import emit_identity_binding

        def boom(*args, **kwargs):
            raise RuntimeError("sink is down")

        monkeypatch.setattr(AnalyticsEvent.objects, "create", boom)
        with _capturing("users.identity.events", caplog):
            emit_identity_binding(
                actor=None, external_user_id=EXT_ID,
                result="rejected", reason="target_not_bindable", initiator="test",
            )

        captured = _text(caplog)
        assert "binding_audit_failed" in captured, "факт события потерян"
        assert EXT_ID not in captured


class TestThePhoneBinding:
    """``users/social_auth.py`` — привязка телефона к учётной записи."""

    def test_the_phone_is_not_logged(self, caplog, real_user, settings):
        from users.social_auth import SocialAuthService

        settings.DEBUG = True
        settings.SMS_ENABLED = False
        OTPCode.objects.create(
            phone=PHONE, code="0000",
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        with _capturing("users.social_auth", caplog):
            SocialAuthService().bind_phone(real_user, PHONE, "0000")

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert PHONE not in captured
        assert PHONE.lstrip("+") not in captured
        assert str(real_user.pk) in captured, "зацепка потеряна вместе с номером"


class TestTheNutritionProfile:
    """``nutrition/views.py`` — самая дешёвая из семи ветвей: отказ по согласию."""

    @pytest.fixture
    def api(self, settings, db):
        settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
        User.objects.create(username=EXT_ID, role="client", is_proxy=True)
        return APIClient()

    def _post_without_consent(self, api):
        return api.post(
            URL_PROFILE, FULL_INPUTS, format="json",
            **{
                "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
                "HTTP_X_EXTERNAL_USER_ID": EXT_ID,
            },
        )

    def test_the_external_id_is_not_logged(self, caplog, api):
        with _capturing("nutrition.views", caplog):
            self._post_without_consent(api)

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert EXT_ID not in captured

    def test_the_internal_pk_is_logged_instead(self, caplog, api):
        with _capturing("nutrition.views", caplog):
            self._post_without_consent(api)

        user = User.objects.get(username=EXT_ID)
        assert str(user.pk) in _text(caplog)


class TestTheCensus:
    """Перепись по AST: личность не передаётся аргументом ``logger.*``.

    Считается по дереву разбора, а не построчно: вызов, открывающий скобку в
    конце строки, для построчного шаблона невидим — в этом дереве таких
    большинство, и пустой результат читался бы как чистота.
    """

    #: ``username`` здесь не случайно: прокси-строка ищется ПО НЕМУ, то есть
    #: это снимаемое значение под другим именем, а не зацепка.
    FORBIDDEN = {"external_user_id", "phone", "username"}
    LEVELS = {"info", "warning", "warn", "error", "debug", "exception", "critical"}

    def _logger_calls(self, tree):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in self.LEVELS):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "logger"):
                continue
            yield node

    def _scan(self):
        import nutrition.views as anchor

        root = Path(anchor.__file__).resolve().parent.parent
        seen, offenders = 0, []
        for rel in MODULES:
            source = root / rel
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in self._logger_calls(tree):
                seen += 1
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if isinstance(arg, ast.Name) and arg.id in self.FORBIDDEN:
                        offenders.append(f"{rel}:{node.lineno} -> {arg.id}")
                    elif isinstance(arg, ast.Attribute) and arg.attr in self.FORBIDDEN:
                        offenders.append(f"{rel}:{node.lineno} -> {arg.attr}")
        return seen, offenders

    def test_no_identity_is_passed_as_a_logger_argument(self):
        _, offenders = self._scan()
        assert not offenders, "личность в аргументах логирования: " + "; ".join(offenders)

    def test_the_scan_is_not_vacuous(self):
        """Нижняя граница: пустой скан обязан отличаться от чистого."""
        seen, _ = self._scan()
        assert seen >= 13, (
            f"перепись увидела {seen} вызовов logger.* в пяти файлах предмета; "
            "их там не меньше тринадцати — значит скан смотрит не туда"
        )
