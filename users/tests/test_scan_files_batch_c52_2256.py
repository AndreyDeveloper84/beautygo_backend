"""C5.2 «забудь всё»: файлы фото сканера — пачкой и до блокировок (DRF-2256).

Путь бота (``InternalPersonalDataDeleteView``) снимал файлы сканов по одному,
тремя вызовами хранилища на файл (``exists`` → ``delete`` → ``exists``), внутри
``atomic()`` и уже после блокировки строки профиля питания
(``select_for_update`` в ``erase_personal_calculation_inputs``). Таймаут бота —
10 с; в проде хранилище S3 (Minio), каждый вызов — сетевой. Сколько сканов у
людей на стенде — не измерено и измерено не будет.

Теперь: имена файлов всех личностей субъекта собираются ДО транзакции, файлы
снимаются одной пачкой (S3 ``delete_objects`` — до 1000 ключей за вызов), и
только потом — транзакция со строками. Порядок «файл раньше строки» сохранён.

* b1 — бюджет: 500 сканов — ОДИН вызов ``delete_objects``, ни одного ``exists``;
* b2 — файлы снимаются до транзакции стирания и до любой блокировки строк
  (открыта только транзакция журнала доступа, §96) и раньше строк: в момент
  вызова хранилища строки сканов на месте;
* b3 — стойкий сбой хранилища (``Errors`` в ответе пачки) → 500 от
  ``IncompleteErasure``, в базе не стёрто ничего; повтор бота (DRF-1950)
  повторит всё;
* b4 — ответ «стёрто» после пачки: строки сканов и остальное удалены;
* b5 — скан, появившийся между пачкой и транзакцией, не оставляет файла:
  его файл снимается старым путём внутри транзакции.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from nutrition.models import FoodScan, NutritionProfile
from users.tests.test_forget_all_diary_2214 import _bot_url
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    _internal,
    _set_token,
    user,
)

pytestmark = pytest.mark.django_db


class _FakeBucket:
    def __init__(self, owner: "_FakeS3", errors: list[dict] | None = None) -> None:
        self.owner = owner
        self.errors = errors or []

    def delete_objects(self, Delete: dict) -> dict:  # noqa: N803 — имя параметра boto3
        self.owner.calls.append(("delete_objects", len(Delete["Objects"])))
        # Глубина вложенности транзакций: тест django_db сам идёт внутри
        # atomic(), поэтому признак in_atomic_block здесь всегда True.
        self.owner.atomic_depth.append(len(connection.atomic_blocks))
        if self.owner.queries is not None:
            self.owner.queries_at_call.append(len(self.owner.queries.captured_queries))
        self.owner.rows_at_call.append(FoodScan.objects.count())
        if self.errors:
            return {"Errors": self.errors}
        for obj in Delete["Objects"]:
            self.owner.files.discard(obj["Key"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete["Objects"]]}


class _FakeS3:
    """Хранилище в форме S3Boto3Storage: ``bucket.delete_objects`` и ``exists``."""

    def __init__(self, names: set[str], *, errors: list[dict] | None = None) -> None:
        self.files = set(names)
        self.calls: list[tuple] = []
        self.atomic_depth: list[int] = []
        self.queries = None  # CaptureQueriesContext, если тест следит за блокировками
        self.queries_at_call: list[int] = []
        self.rows_at_call: list[int] = []
        self.bucket = _FakeBucket(self, errors)

    def _normalize_name(self, name: str) -> str:
        return name

    def exists(self, name: str) -> bool:
        self.calls.append(("exists", name))
        return name in self.files

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))
        self.files.discard(name)


def _seed_scans(u, n: int) -> list[str]:
    names = [f"food-scans/{u.pk}/{i:05d}.jpg" for i in range(n)]
    FoodScan.objects.bulk_create(
        [FoodScan(user=u, dish_name="x", provider_used=FoodScan.Provider.OPENAI, image=name) for name in names]
    )
    return names


@pytest.fixture
def fake_storage(monkeypatch):
    holder = SimpleNamespace(storage=None)

    def install(storage: _FakeS3) -> _FakeS3:
        field = FoodScan._meta.get_field("image")
        monkeypatch.setattr(field, "storage", storage)
        holder.storage = storage
        return storage

    return install


class TestB1Budget:
    def test_500_scans_are_one_batch_call_and_no_exists(self, user, fake_storage) -> None:  # noqa: F811
        names = _seed_scans(user, 500)
        storage = fake_storage(_FakeS3(set(names)))
        resp = _internal().delete(_bot_url(user))
        assert resp.status_code == 200, resp.content
        assert storage.calls == [("delete_objects", 500)]
        assert storage.files == set()


class TestB2OutsideTheTransactionAndBeforeRows:
    def test_files_go_first_and_outside_atomic(self, user, fake_storage) -> None:  # noqa: F811
        names = _seed_scans(user, 3)
        # Профиль питания — строка, которую стирание блокирует (DRF-2256).
        NutritionProfile.objects.create(
            user=user, weight_kg=60, height_cm=165, age=30, gender="female",
            activity_coefficient=1.6, goal="lose", daily_kcal=1800,
        )
        storage = fake_storage(_FakeS3(set(names)))
        outer = len(connection.atomic_blocks)  # транзакция самого теста
        with CaptureQueriesContext(connection) as queries:
            storage.queries = queries
            assert _internal().delete(_bot_url(user)).status_code == 200
        # Открыта ровно одна транзакция — журнала доступа
        # (``privacy_audit.mixins``: запись журнала и стирание — одна
        # транзакция, §96); своя транзакция вью ещё не открыта.
        assert storage.atomic_depth == [outer + 1]
        # Ни одной блокировки строк до пачки; после — есть (профиль питания),
        # иначе проверка «до» была бы пустой.
        (at_call,) = storage.queries_at_call
        sql = [q["sql"].upper() for q in queries.captured_queries]
        assert any("FOR UPDATE" in q for q in sql[at_call:])
        assert not any("FOR UPDATE" in q for q in sql[:at_call])
        assert storage.rows_at_call == [3]  # строки ещё на месте — файл раньше строки


class TestB3PersistentStorageFailure:
    def test_errors_in_the_batch_are_a_500_and_nothing_is_erased(self, user, fake_storage) -> None:  # noqa: F811
        names = _seed_scans(user, 3)
        fake_storage(_FakeS3(set(names), errors=[{"Key": names[0], "Code": "InternalError"}]))
        client = _internal()
        client.raise_request_exception = False
        resp = client.delete(_bot_url(user))
        assert resp.status_code == 500
        assert FoodScan.objects.filter(user=user).count() == 3


class TestB4RowsAreGoneAfterTheBatch:
    def test_scan_rows_are_erased(self, user, fake_storage) -> None:  # noqa: F811
        names = _seed_scans(user, 5)
        fake_storage(_FakeS3(set(names)))
        assert _internal().delete(_bot_url(user)).status_code == 200
        assert FoodScan.objects.filter(user=user).count() == 0


class TestB5LateScanLeavesNoFile:
    def test_a_scan_created_after_the_batch_is_removed_inside(
        self, user, fake_storage, monkeypatch  # noqa: F811
    ) -> None:
        names = _seed_scans(user, 2)
        storage = fake_storage(_FakeS3(set(names) | {f"food-scans/{user.pk}/late.jpg"}))
        from users import scan_file_erasure

        original = scan_file_erasure.remove_scan_files

        def _then_a_late_scan(*args, **kwargs):
            removed = original(*args, **kwargs)
            FoodScan.objects.create(
                user=user, dish_name="late", provider_used=FoodScan.Provider.OPENAI,
                image=f"food-scans/{user.pk}/late.jpg",
            )
            return removed

        monkeypatch.setattr("users.personal_data_api.remove_scan_files", _then_a_late_scan)
        assert _internal().delete(_bot_url(user)).status_code == 200
        assert storage.files == set()
        assert FoodScan.objects.filter(user=user).count() == 0
