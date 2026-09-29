"""DRF-2457 — медиа (фото клиентов, еды, мастеров) не лежит в открытом бакете.

Публичность держалась двумя механизмами, и закрывать надо оба:

* ``STORAGES["default"]["OPTIONS"]["default_acl"] = "public-read"`` в
  ``settings/dev.py`` и ``settings/prod.py`` — ACL каждого загружаемого объекта;
* ``mc anonymous set download local/beautygo-media`` в ``minio-init``
  (``docker-compose.yml``) — ПОЛИТИКА БАКЕТА «анонимное чтение». Она
  повторялась при каждом ``compose up``: закрытый руками бакет следующая
  выкладка открывала снова.

Показу публичность не нужна: ``FieldFile.url`` подписан (``querystring_auth``
— умолчание django-storages), а фото мастера и снимок еды бот получает байтами
через бэкенд (DRF-2539, DRF-2455).

Узлы держат литерал ``"public-read"`` / ``anonymous set download`` — не
константу из охраняемого файла: узел, читающий значение из того же места,
которое проверяет, прошёл бы при самом дефекте.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from storages.backends.s3boto3 import S3Boto3Storage

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ("djangoProject/settings/dev.py", "djangoProject/settings/prod.py")
COMPOSE = ROOT / "docker-compose.yml"


def _default_storage_options(rel: str) -> dict[str, object]:
    """``STORAGES["default"]["OPTIONS"]`` файла настроек — литералы по AST."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "STORAGES" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            storages = {ast.literal_eval(k): v for k, v in zip(node.value.keys, node.value.values)}
            default = storages["default"]
            assert isinstance(default, ast.Dict)
            inner = {ast.literal_eval(k): v for k, v in zip(default.keys, default.values)}
            options = inner["OPTIONS"]
            assert isinstance(options, ast.Dict)
            out: dict[str, object] = {}
            for k, v in zip(options.keys, options.values):
                try:
                    out[ast.literal_eval(k)] = ast.literal_eval(v)
                except ValueError:
                    out[ast.literal_eval(k)] = "<выражение>"
            return out
    raise AssertionError(f"{rel}: STORAGES не найден")


def _minio_init_entrypoint() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"^  minio-init:\n(.*?)(?=^\S|^  \w[\w-]*:\n)", text, re.M | re.S)
    assert m, "docker-compose.yml: сервис minio-init не найден"
    return m.group(1)


class TestObjectsAreNotPublic:
    def test_default_storage_acl_is_not_public_read_in_dev_and_prod(self) -> None:
        for rel in SETTINGS:
            options = _default_storage_options(rel)
            # Положительный контроль: ключ найден — иначе проверка ниже пуста.
            assert "default_acl" in options, rel
            assert options["default_acl"] != "public-read", rel
            assert options["default_acl"] == "private", rel

    def test_minio_bucket_is_not_anonymous(self) -> None:
        entry = _minio_init_entrypoint()
        # Положительный контроль: блок тот — бакет в нём создаётся.
        assert "mc mb --ignore-existing local/beautygo-media" in entry
        assert "mc anonymous set download" not in entry
        assert "mc anonymous set none local/beautygo-media" in entry


class TestDisplayStillWorks:
    def test_file_url_under_the_prod_options_is_signed(self) -> None:
        """Показ опирается на подпись, а не на публичность: адрес подписан и
        при закрытом бакете открывается. Подпись считается офлайн — сеть и
        MinIO не нужны."""
        options = _default_storage_options("djangoProject/settings/prod.py")
        storage = S3Boto3Storage(
            access_key="test-access",
            secret_key="test-secret",  # noqa: S106  # pragma: allowlist secret — подпись офлайн
            bucket_name="beautygo-media",
            endpoint_url="http://minio:9000",
            custom_domain=options["custom_domain"],
            default_acl=options["default_acl"],
            file_overwrite=options["file_overwrite"],
        )
        url = storage.url("specialists/avatars/a.jpg")
        assert url.startswith("http://minio:9000/beautygo-media/specialists/avatars/a.jpg?")
        assert re.search(r"(X-Amz-Signature|Signature)=", url), url
