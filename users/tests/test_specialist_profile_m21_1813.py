"""DRF-1813 (M21): профиль мастера, аватар и портфолио под субъектом (P57, P59–P62).

``/api/v1/internal/specialists/{id}/profile/`` (GET/PATCH),
``…/media/avatar/`` (POST/DELETE), ``…/portfolio/`` (GET/POST),
``…/portfolio/{item_id}/`` (DELETE) под ``IsInternalBearerForSpecialistSubject``
и журналом §96 (``AuditedPersonalDataAccess``). Что стережётся:

* имя — не короче 2 символов; «о себе» — не длиннее 500 (один лимит);
  профиль без фото сохраняется (требование фото — в готовности M4);
* аватар: JPEG / PNG / WebP по содержимому, не по заголовку; ≤ 5 МБ;
  квадрат с допуском 2 %; замена удаляет прежний файл; удаление снимает
  и поле, и файл;
* портфолио: не больше 10 — одиннадцатое 400 с именем причины; чужой
  элемент — 404, не удаляется;
* чужой workspace — 403;
* каждая операция пишет строку журнала своего имени; удаление фото —
  разрушающая операция: без записи в журнал не происходит.

Каждый отказ рядом с положительной половиной на тех же данных.
Красный до правки: весь файл (маршрутов, операций журнала и политики нет).
"""

from __future__ import annotations

import io
import uuid

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.policy import stops_when_unauditable
from tenants.solo_provisioning import provision_solo_workspace
from users.models import SpecialistPortfolio, SpecialistProfile

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1813"  # noqa: S105
OLGA = "bot:max:1813001"
IRINA = "bot:max:1813002"


@pytest.fixture(autouse=True)
def _settings(settings, tmp_path):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.MEDIA_ROOT = str(tmp_path)


def _provision(external_user_id: str, slug: str) -> SpecialistProfile:
    return provision_solo_workspace(
        tenant_id=uuid.uuid4(),
        slug=slug,
        name=f"Студия {slug}",
        city="Пенза",
        external_user_id=external_user_id,
        display_name="Мастер",
    ).profile


@pytest.fixture
def olga() -> SpecialistProfile:
    return _provision(OLGA, "solo-max-1813olga")


@pytest.fixture
def irina() -> SpecialistProfile:
    return _provision(IRINA, "solo-max-1813irin")


def _client(actor: str = OLGA) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _profile_url(profile) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/profile/"


def _avatar_url(profile) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/media/avatar/"


def _portfolio_url(profile) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/portfolio/"


def _image(width: int = 100, height: int = 100, fmt: str = "JPEG", pad_to: int = 0) -> SimpleUploadedFile:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="red").save(buf, format=fmt)
    payload = buf.getvalue()
    if pad_to > len(payload):
        payload += b"\x00" * (pad_to - len(payload))
    name, content_type = {
        "JPEG": ("a.jpg", "image/jpeg"),
        "PNG": ("a.png", "image/png"),
        "WEBP": ("a.webp", "image/webp"),
        "GIF": ("a.gif", "image/gif"),
    }[fmt]
    return SimpleUploadedFile(name, payload, content_type=content_type)


def _upload(url: str, image: SimpleUploadedFile, actor: str = OLGA):
    return _client(actor).post(url, {"image": image}, format="multipart")


def _journal(profile, operation: str):
    return PersonalDataAccessLog.objects.filter(object_id=profile.pk, operation=operation)


class TestProfile:
    def test_get_reads_the_profile(self, olga):
        r = _client().get(_profile_url(olga))

        assert r.status_code == 200, r.content
        data = r.json()["data"]
        assert data["specialist_id"] == str(olga.pk)
        assert data["display_name"] == "Мастер"
        assert data["bio"] == ""
        assert data["avatar_url"] == ""
        assert data["portfolio"] == {"count": 0, "limit": 10}

    def test_patch_writes_the_name_and_the_bio(self, olga):
        r = _client().patch(
            _profile_url(olga), {"display_name": "Ольга Петрова", "bio": "б" * 500}, format="json",
        )

        assert r.status_code == 200, r.content
        olga.refresh_from_db()
        assert olga.display_name == "Ольга Петрова"
        assert olga.bio == "б" * 500

    def test_a_501_character_bio_is_400_and_nothing_changes(self, olga):
        r = _client().patch(_profile_url(olga), {"bio": "б" * 501}, format="json")

        assert r.status_code == 400, r.content
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "bio" in r.json()["error"]["details"]
        olga.refresh_from_db()
        assert olga.bio == ""
        # Положительная половина: ровно 500 — сохраняется.
        assert _client().patch(_profile_url(olga), {"bio": "б" * 500}, format="json").status_code == 200

    def test_a_name_shorter_than_two_characters_is_400(self, olga):
        r = _client().patch(_profile_url(olga), {"display_name": " О "}, format="json")

        assert r.status_code == 400, r.content
        assert "display_name" in r.json()["error"]["details"]
        olga.refresh_from_db()
        assert olga.display_name == "Мастер"
        assert _client().patch(_profile_url(olga), {"display_name": "Ол"}, format="json").status_code == 200

    def test_a_profile_without_a_photo_is_saved(self, olga):
        assert not olga.avatar

        r = _client().patch(_profile_url(olga), {"bio": "Маникюр с 2015 года"}, format="json")

        assert r.status_code == 200, r.content
        assert r.json()["data"]["avatar_url"] == ""


class TestAvatar:
    def test_a_square_photo_is_accepted_and_replaces_the_previous_file(
        self, olga, django_capture_on_commit_callbacks,
    ):
        first = _upload(_avatar_url(olga), _image(100, 100))
        assert first.status_code == 200, first.content
        olga.refresh_from_db()
        old_name = olga.avatar.name
        assert old_name and default_storage.exists(old_name)
        assert first.json()["data"]["avatar_url"]

        # Прежний файл удаляется после фиксации транзакции — колбэки фиксации
        # тестовая транзакция сама не выполняет.
        with django_capture_on_commit_callbacks(execute=True):
            second = _upload(_avatar_url(olga), _image(120, 120, fmt="PNG"))

        assert second.status_code == 200, second.content
        olga.refresh_from_db()
        assert olga.avatar.name != old_name
        assert default_storage.exists(olga.avatar.name)
        assert not default_storage.exists(old_name)

    def test_a_photo_off_square_by_more_than_two_percent_is_400(self, olga):
        r = _upload(_avatar_url(olga), _image(100, 90))

        assert r.status_code == 400, r.content
        assert r.json()["error"]["details"]["reason"] == "not_square"
        olga.refresh_from_db()
        assert not olga.avatar
        # Положительная половина: 2 % допуска — принимается.
        assert _upload(_avatar_url(olga), _image(100, 98)).status_code == 200

    @pytest.mark.parametrize(
        "image, reason",
        [
            (lambda: _image(fmt="GIF"), "unsupported_type"),
            (lambda: SimpleUploadedFile("a.jpg", b"not an image at all", content_type="image/jpeg"), "not_an_image"),
            (lambda: _image(pad_to=5 * 1024 * 1024 + 1), "file_too_large"),
        ],
        ids=["gif", "declared-jpeg-but-not-an-image", "over-5-mb"],
    )
    def test_a_bad_file_is_400_with_its_reason(self, olga, image, reason):
        r = _upload(_avatar_url(olga), image())

        assert r.status_code == 400, r.content
        assert r.json()["error"]["details"]["reason"] == reason
        olga.refresh_from_db()
        assert not olga.avatar

    def test_a_missing_file_is_400(self, olga):
        r = _client().post(_avatar_url(olga), {}, format="multipart")

        assert r.status_code == 400, r.content
        assert r.json()["error"]["details"]["reason"] == "image_required"

    def test_delete_removes_the_field_and_the_file(self, olga, django_capture_on_commit_callbacks):
        assert _upload(_avatar_url(olga), _image()).status_code == 200
        olga.refresh_from_db()
        name = olga.avatar.name

        with django_capture_on_commit_callbacks(execute=True):
            r = _client().delete(_avatar_url(olga))

        assert r.status_code == 200, r.content
        assert r.json()["data"]["avatar_url"] == ""
        olga.refresh_from_db()
        assert not olga.avatar
        assert not default_storage.exists(name)


class TestPortfolio:
    def test_upload_list_and_delete(self, olga, django_capture_on_commit_callbacks):
        created = _upload(_portfolio_url(olga), _image(80, 60))
        assert created.status_code == 201, created.content
        item_id = created.json()["data"]["id"]
        item = SpecialistPortfolio.objects.get(pk=item_id)
        name = item.image.name
        assert default_storage.exists(name)

        listed = _client().get(_portfolio_url(olga))
        assert listed.status_code == 200, listed.content
        assert listed.json()["data"]["limit"] == 10
        assert [i["id"] for i in listed.json()["data"]["items"]] == [item_id]

        with django_capture_on_commit_callbacks(execute=True):
            deleted = _client().delete(f"{_portfolio_url(olga)}{item_id}/")
        assert deleted.status_code == 200, deleted.content
        assert deleted.json()["data"]["count"] == 0
        assert not SpecialistPortfolio.objects.filter(pk=item_id).exists()
        assert not default_storage.exists(name)

    def test_the_eleventh_photo_is_400_with_the_reason(self, olga):
        for _ in range(10):
            assert _upload(_portfolio_url(olga), _image(40, 40)).status_code == 201

        r = _upload(_portfolio_url(olga), _image(40, 40))

        assert r.status_code == 400, r.content
        assert r.json()["error"]["details"] == {"reason": "portfolio_limit_exceeded", "limit": 10}
        assert SpecialistPortfolio.objects.filter(specialist=olga).count() == 10
        # Положительная половина: одно убрали — одиннадцатое место свободно.
        first = SpecialistPortfolio.objects.filter(specialist=olga).first()
        assert _client().delete(f"{_portfolio_url(olga)}{first.pk}/").status_code == 200
        assert _upload(_portfolio_url(olga), _image(40, 40)).status_code == 201

    def test_another_masters_item_is_404_and_stays(self, olga, irina):
        foreign = _upload(_portfolio_url(irina), _image(40, 40), actor=IRINA)
        assert foreign.status_code == 201, foreign.content
        foreign_id = foreign.json()["data"]["id"]

        r = _client().delete(f"{_portfolio_url(olga)}{foreign_id}/")

        assert r.status_code == 404, r.content
        assert SpecialistPortfolio.objects.filter(pk=foreign_id).exists()

    def test_a_bad_portfolio_file_is_400(self, olga):
        r = _upload(_portfolio_url(olga), _image(fmt="GIF"))

        assert r.status_code == 400, r.content
        assert r.json()["error"]["details"]["reason"] == "unsupported_type"
        assert SpecialistPortfolio.objects.filter(specialist=olga).count() == 0


class TestSubject:
    def test_another_masters_workspace_is_403(self, olga, irina):
        assert _client().get(_profile_url(irina)).status_code == 403
        assert _client().patch(_profile_url(irina), {"bio": "x"}, format="json").status_code == 403
        assert _upload(_avatar_url(irina), _image()).status_code == 403
        assert _client().get(_portfolio_url(irina)).status_code == 403
        irina.refresh_from_db()
        assert irina.bio == "" and not irina.avatar
        # Положительная половина: свой — можно.
        assert _client().get(_profile_url(olga)).status_code == 200


class TestJournal:
    def test_every_operation_writes_its_own_journal_row(self, olga):
        assert _client().get(_profile_url(olga)).status_code == 200
        assert _client().patch(_profile_url(olga), {"bio": "x"}, format="json").status_code == 200
        assert _upload(_avatar_url(olga), _image()).status_code == 200
        assert _client().delete(_avatar_url(olga)).status_code == 200

        for operation in ("read_profile", "write_specialist_profile", "upload_media", "delete_media"):
            rows = _journal(olga, operation)
            assert rows.count() == 1, operation
            assert rows.get().object_category == "specialist_profile"
            assert rows.get().result == PersonalDataAccessLog.Result.ALLOWED

    def test_deleting_a_photo_does_not_happen_without_a_journal_row(self):
        """Удаление фото — разрушающая операция (§96 / §107: «удаление» в
        закрытом списке владельца): без записи в журнал оно не происходит.
        Запись и загрузка — нет, как запись в личный профиль. Решение
        главного окна 15.09: основание — DELETE уже в закрытом списке."""

        assert stops_when_unauditable("delete_media") is True
        assert stops_when_unauditable("upload_media") is False
        assert stops_when_unauditable("write_specialist_profile") is False

    def test_an_avatar_delete_during_a_journal_outage_is_503_and_nothing_is_lost(
        self, olga, broken_journal, django_capture_on_commit_callbacks,
    ):
        """Файл удаляется из хранилища только после фиксации транзакции:
        журнал упал — транзакция откатилась — и поле, и файл на месте."""

        broken_journal.off()
        assert _upload(_avatar_url(olga), _image()).status_code == 200
        olga.refresh_from_db()
        name = olga.avatar.name
        broken_journal.on()

        # Колбэки фиксации выполняются только у зафиксированной транзакции:
        # откаченная не оставляет ни одного — файл обязан уцелеть.
        with django_capture_on_commit_callbacks(execute=True):
            r = _client().delete(_avatar_url(olga))

        assert r.status_code == 503, r.content
        assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        olga.refresh_from_db()
        assert olga.avatar.name == name
        assert default_storage.exists(name)
        # Повтор безопасен: журнал вернулся — удаление проходит.
        broken_journal.off()
        with django_capture_on_commit_callbacks(execute=True):
            assert _client().delete(_avatar_url(olga)).status_code == 200
        assert not default_storage.exists(name)

    def test_a_portfolio_delete_during_a_journal_outage_is_503_and_the_item_stays(
        self, olga, broken_journal,
    ):
        broken_journal.off()
        created = _upload(_portfolio_url(olga), _image(40, 40))
        assert created.status_code == 201, created.content
        item = SpecialistPortfolio.objects.get(pk=created.json()["data"]["id"])
        broken_journal.on()

        r = _client().delete(f"{_portfolio_url(olga)}{item.pk}/")

        assert r.status_code == 503, r.content
        assert SpecialistPortfolio.objects.filter(pk=item.pk).exists()
        assert default_storage.exists(item.image.name)

    def test_an_upload_during_a_journal_outage_still_happens(self, olga, broken_journal):
        """Вторая сторона черты §107: не разрушающая операция не останавливает
        человека, действующего со своими данными — потеря строки журнала
        пишется ERROR, фото сохраняется."""

        broken_journal.on()

        r = _upload(_avatar_url(olga), _image())

        assert r.status_code == 200, r.content
        olga.refresh_from_db()
        assert olga.avatar and default_storage.exists(olga.avatar.name)


@pytest.fixture
def broken_journal(monkeypatch):
    """Как в ``privacy_audit/tests/test_access_journal.py``: прямая запись в
    журнал падает, маршрутизация §107 (остановить или потерять с ERROR)
    проходит по-настоящему. ``on()`` / ``off()`` — чтобы подготовить данные
    при живом журнале в том же тесте."""

    from privacy_audit import services
    from privacy_audit.services import AuditUnavailable

    original = services.record_access

    def _boom(**_kwargs):
        raise AuditUnavailable("simulated journal outage")

    class _Switch:
        def on(self):
            monkeypatch.setattr(services, "record_access", _boom)

        def off(self):
            monkeypatch.setattr(services, "record_access", original)

    return _Switch()
