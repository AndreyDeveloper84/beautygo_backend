"""Фото мастера и работы портфолио — байты для бота, а не адрес хранилища (DRF-2539).

Узлы:

* f1 — аватар есть: 200 и БАЙТЫ файла, тип ``image/*``, ``private``;
* f2 — аватара нет / мастера нет / объект пропал / объект пуст: одинаковый 404;
* f3 — без сервисного токена файл не отдаётся;
* f4 — работа портфолио своего мастера: 200 и байты;
* f5 — работа ЧУЖОГО мастера под адресом этого: 404. Ручка, не видевшая
  чужого запроса, не доказана — подмена «снять фильтр по мастеру» краснит f5.
"""

from __future__ import annotations

import io
import random
import uuid

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from tenants.solo_provisioning import provision_solo_workspace
from users.models import SpecialistPortfolio, SpecialistProfile

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-2539"  # noqa: S105


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
    return _provision("bot:max:2539001", "solo-max-2539olga")


@pytest.fixture
def irina() -> SpecialistProfile:
    return _provision("bot:max:2539002", "solo-max-2539irin")


def _photo(name: str = "a.png") -> SimpleUploadedFile:
    """Настоящий PNG правдоподобного размера: шум не сжимается (как в DRF-2455)."""
    side = 120
    noise = random.Random(2539).randbytes(side * side * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (side, side), noise).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def _bot() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    return c


def _avatar_file(profile) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/media/avatar/file/"


def _portfolio_file(profile, item) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/portfolio/{item.pk}/file/"


def _body(resp) -> bytes:
    return b"".join(resp.streaming_content) if resp.streaming else resp.content


def _with_avatar(profile: SpecialistProfile) -> bytes:
    upload = _photo()
    payload = upload.read()
    upload.seek(0)
    profile.avatar = upload
    profile.save(update_fields=["avatar"])
    return payload


class TestAvatar:
    def test_f1_the_file_itself_comes_back(self, olga):
        _with_avatar(olga)
        stored = olga.avatar.read()

        resp = _bot().get(_avatar_file(olga))

        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("image/")
        assert resp["Cache-Control"] == "private, max-age=300"
        body = _body(resp)
        assert len(body) > 1024
        assert body == stored

    def test_f2_no_avatar_is_404(self, olga):
        assert not olga.avatar
        assert _bot().get(_avatar_file(olga)).status_code == 404

    def test_f2_unknown_specialist_is_404(self, olga):
        resp = _bot().get(f"/api/v1/internal/specialists/{uuid.uuid4()}/media/avatar/file/")
        assert resp.status_code == 404

    def test_f2_dangling_object_is_404_not_500(self, olga):
        _with_avatar(olga)
        default_storage.delete(olga.avatar.name)
        assert _bot().get(_avatar_file(olga)).status_code == 404

    def test_f2_empty_object_is_404(self, olga):
        olga.avatar = SimpleUploadedFile("tiny.png", b"\x89PNG" + b"\x00" * 200, content_type="image/png")
        olga.save(update_fields=["avatar"])
        assert olga.avatar.size < 1024
        assert _bot().get(_avatar_file(olga)).status_code == 404

    def test_f3_no_service_token_no_file(self, olga):
        _with_avatar(olga)
        resp = APIClient().get(_avatar_file(olga))
        assert resp.status_code in (401, 403)
        assert b"PNG" not in _body(resp)


class TestPortfolio:
    def test_f4_own_work_comes_back(self, olga):
        item = SpecialistPortfolio.objects.create(specialist=olga, image=_photo("w.png"))
        stored = item.image.read()

        resp = _bot().get(_portfolio_file(olga, item))

        assert resp.status_code == 200
        assert _body(resp) == stored

    def test_f5_another_masters_work_under_this_address_is_404(self, olga, irina):
        theirs = SpecialistPortfolio.objects.create(specialist=irina, image=_photo("w.png"))
        # Положительная половина: под своим адресом работа отдаётся.
        assert _bot().get(_portfolio_file(irina, theirs)).status_code == 200

        resp = _bot().get(_portfolio_file(olga, theirs))

        assert resp.status_code == 404
