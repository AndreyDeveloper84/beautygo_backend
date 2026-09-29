"""DRF-2619 — фото мастера рукой администратора салона: байты у каталога.

Узлы — пары, которые обязаны различаться:

* администратор СВОЕГО мастера грузит — администратор ЧУЖОГО салона получает
  отказ. «Загрузка работает» в одиночку прошла бы и при снятой проверке
  принадлежности мастера салону;
* собственный токен администратора принят — обычный JWT того же человека и
  служебный ключ нет: у двери одна форма опознания (DRF-2607);
* после загрузки у профиля ровно загруженные байты — у профиля без загрузки
  фото нет.

Все запросы — через настоящий стек аутентификации, без ``force_authenticate``.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from privacy_audit.models import PersonalDataAccessLog
from tenants.models import Tenant
from users.max_salon_admin_auth import issue_salon_admin_token
from users.models import SpecialistProfile, TenantUserRelationship, User

SERVICE_TOKEN = "ayla-service-token-under-test-2619"  # pragma: allowlist secret

_Log = PersonalDataAccessLog


@pytest.fixture(autouse=True)
def _env(settings, tmp_path):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN
    settings.MEDIA_ROOT = str(tmp_path)


def _png(size=(64, 64), color=(200, 30, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _upload(content: bytes, name: str = "face.png"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content, content_type="image/png")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="s2619-a", name="Salon A")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="s2619-b", name="Salon B")


def _admin_of(tenant: Tenant, username: str, phone: str) -> User:
    user = User.objects.create_user(username=username, password="x", role="client", phone=phone)
    TenantUserRelationship.objects.create(
        user=user, tenant=tenant, role=TenantUserRelationship.Role.ADMIN, is_active=True
    )
    return user


@pytest.fixture
def admin(salon):
    return _admin_of(salon, "bot:max:26190001", "+79995261901")


@pytest.fixture
def foreign_admin(other_salon):
    return _admin_of(other_salon, "bot:max:26190002", "+79995261902")


@pytest.fixture
def master(salon):
    user = User.objects.create_user(username="m2619", password="x", role="specialist", phone="+79995261909")
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.save(update_fields=["tenant"])
    return profile


def _post(master, *, tenant: Tenant, bearer: str | None, content: bytes | None = None):
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.defaults["HTTP_X_TENANT"] = tenant.slug
    if bearer is not None:
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    body = {"image": _upload(_png() if content is None else content)}
    return client.post(
        f"/api/v1/tenants/me/masters/{master.id}/media/avatar/", body, format="multipart"
    )


def _token(user: User, tenant: Tenant) -> str:
    return str(issue_salon_admin_token(user, tenant))


def _stored_bytes(profile: SpecialistProfile) -> bytes:
    profile.refresh_from_db()
    if not profile.avatar:
        return b""
    with profile.avatar.open("rb") as fh:
        return fh.read()


@pytest.mark.django_db
class TestOwnMasterUploadsForeignSalonIsRefused:
    def test_the_admin_of_the_masters_salon_uploads_the_exact_bytes(self, admin, salon, master):
        content = _png(color=(10, 120, 240))
        assert _stored_bytes(master) == b""  # до загрузки фото нет

        resp = _post(master, tenant=salon, bearer=_token(admin, salon), content=content)

        assert resp.status_code == 200, resp.content
        assert _stored_bytes(master) == content
        assert resp.data["data"]["avatar_url"].endswith(master.avatar.name.split("/")[-1])

    def test_the_admin_of_another_salon_is_refused_and_nothing_is_written(
        self, foreign_admin, other_salon, master
    ):
        # Токен честный — для своего салона Б; мастер — салона А.
        resp = _post(master, tenant=other_salon, bearer=_token(foreign_admin, other_salon))

        assert resp.status_code == 404, resp.content
        assert _stored_bytes(master) == b""

    def test_a_token_of_salon_b_presented_for_salon_a_is_refused(
        self, foreign_admin, salon, other_salon, master
    ):
        resp = _post(master, tenant=salon, bearer=_token(foreign_admin, other_salon))
        assert resp.status_code == 401, resp.content
        assert _stored_bytes(master) == b""


@pytest.mark.django_db
class TestOnlyTheAdministratorsOwnToken:
    def test_the_same_person_with_an_ordinary_jwt_is_refused(self, admin, salon, master):
        accepted = _post(master, tenant=salon, bearer=_token(admin, salon))
        refused = _post(master, tenant=salon, bearer=str(AccessToken.for_user(admin)))
        assert (accepted.status_code, refused.status_code) == (200, 401)

    def test_the_service_key_is_refused(self, salon, master):
        resp = _post(master, tenant=salon, bearer=SERVICE_TOKEN)
        assert resp.status_code == 401
        assert _stored_bytes(master) == b""

    def test_a_token_without_the_admin_link_writes_nothing(self, salon, master):
        # Токен выпущен (напр. связь сняли после обмена) — права проверяются
        # на каждом запросе.
        person = User.objects.create_user(
            username="bot:max:26190003", password="x", role="client", phone="+79995261903"
        )
        resp = _post(master, tenant=salon, bearer=_token(person, salon))
        assert resp.status_code == 403
        assert _stored_bytes(master) == b""


@pytest.mark.django_db
class TestTheCatalogsRulesHold:
    def test_a_non_square_photo_is_refused_by_the_same_rule_as_the_masters_door(
        self, admin, salon, master
    ):
        resp = _post(master, tenant=salon, bearer=_token(admin, salon), content=_png(size=(64, 40)))
        assert resp.status_code == 400
        assert resp.data["error"]["details"]["reason"] == "not_square"
        assert _stored_bytes(master) == b""

    def test_a_replacement_keeps_only_the_new_bytes(self, admin, salon, master):
        first, second = _png(color=(1, 2, 3)), _png(color=(250, 250, 5))
        _post(master, tenant=salon, bearer=_token(admin, salon), content=first)
        resp = _post(master, tenant=salon, bearer=_token(admin, salon), content=second)
        assert resp.status_code == 200
        assert _stored_bytes(master) == second


@pytest.mark.django_db
class TestJournal96:
    def test_an_upload_and_a_foreign_refusal_are_both_journalled_with_the_salon(
        self, admin, foreign_admin, salon, other_salon, master
    ):
        _post(master, tenant=salon, bearer=_token(admin, salon))
        _post(master, tenant=other_salon, bearer=_token(foreign_admin, other_salon))

        rows = list(
            _Log.objects.filter(object_id=master.pk).order_by("occurred_at").values_list(
                "caller_purpose", "actor_id", "tenant_id", "operation", "result", "denial_reason",
            )
        )
        assert rows == [
            (_Log.CallerPurpose.SALON_ADMIN_TOKEN, admin.pk, salon.pk,
             _Log.Operation.UPLOAD_MEDIA, _Log.Result.ALLOWED, ""),
            (_Log.CallerPurpose.SALON_ADMIN_TOKEN, foreign_admin.pk, other_salon.pk,
             _Log.Operation.UPLOAD_MEDIA, _Log.Result.DENIED, "specialist_not_in_salon"),
        ]

    def test_a_person_without_the_admin_link_is_journalled_as_denied(self, salon, master):
        person = User.objects.create_user(
            username="bot:max:26190004", password="x", role="client", phone="+79995261904"
        )
        _post(master, tenant=salon, bearer=_token(person, salon))
        assert list(
            _Log.objects.filter(object_id=master.pk).values_list("actor_id", "result", "denial_reason")
        ) == [(person.pk, _Log.Result.DENIED, "not_salon_admin")]
