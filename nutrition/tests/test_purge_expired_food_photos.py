"""DRF-1651 — удаление фотографий еды по сроку (§134/§135).

Предмет проверок — не арифметика срока, а свойства, каждое из которых
куплено разбором (`docs/MEASURE_FOOD_PHOTO_RETENTION.md`):

* удаляется **объект хранилища**, а не только строка — иначе фотография
  остаётся в бакете навсегда и без ссылки на неё;
* «объекта не было» считается **отдельно** от «удалено» — на пилоте этот
  исход массовый, и сложить их значило бы отчитаться об уничтожении
  фотографий, которых команда не видела;
* отказ хранилища **не снимает строку** — потерять ссылку на живой файл
  хуже, чем оставить просроченную строку;
* запись дневника переживает удаление скана со своими числами;
* молчание про сирот **не читается как «их нет»**.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from nutrition.models import FoodLog, FoodScan

pytestmark = pytest.mark.django_db

#: Однопиксельный PNG — настоящие байты, а не заглушка: тест обязан
#: проверять, что файл ФИЗИЧЕСКИ исчез из хранилища.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _scan(user, *, age_days: int, with_image: bool = True, raw=None) -> FoodScan:
    scan = FoodScan.objects.create(
        user=user,
        dish_name="Борщ",
        confidence=0.9,
        provider_used=FoodScan.Provider.OPENAI,
        raw_response=raw if raw is not None else {"vision": "тарелка борща"},
    )
    if with_image:
        scan.image.save(f"{scan.id}.jpg", ContentFile(PNG), save=True)
    # ``created_at`` — auto_now_add, поэтому возраст ставится после
    # вставки и через .update(), иначе auto_now_add перепишет его обратно
    # и тест молча мерил бы ноль суток.
    FoodScan.objects.filter(pk=scan.pk).update(
        created_at=timezone.now() - timedelta(days=age_days)
    )
    scan.refresh_from_db()
    return scan


def _run(*args) -> str:
    out = StringIO()
    call_command("purge_expired_food_photos", *args, stdout=out, stderr=out)
    return out.getvalue()


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path):
    """Своё хранилище на каждый тест — ``STORAGES`` целиком, не ``MEDIA_ROOT``.

    Первая редакция переопределяла только ``MEDIA_ROOT``. Этого хватало,
    пока хранилище строилось от него; с DRF-1663 (#341) корневой
    ``conftest.py`` подменяет ``STORAGES["default"]`` на ``FileSystemStorage``
    с ЯВНЫМ ``location`` на всю сессию — и ``MEDIA_ROOT`` для него больше
    ничего не значит. Изоляция стала пустой: все тесты сессии писали в один
    каталог, и проверка «сирота одна» падала в CI с числом 9 — чужие
    ``food-scans/*.jpg`` из соседних тестов той же сессии. Верно для
    каталога, бессмысленно для предмета — тот же класс, что 66 файлов в
    первой редакции, только этажом ниже.

    Поэтому подмена — той же формы, что корневая, но на функцию: свой
    ``location`` на каждый тест, ``setting_changed`` пересобирает
    ``default_storage``. Корневая сессионная остаётся страховкой от записи
    в настоящий бакет; эта — от соседей по сессии.
    """
    import copy

    storages_cfg = copy.deepcopy(settings.STORAGES)
    storages_cfg["default"] = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": str(tmp_path)},
    }
    settings.STORAGES = storages_cfg
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create(username="photo-owner")


class TestTheObjectGoesNotOnlyTheRow:
    """Главное свойство. ``FileField`` не трогает объект при удалении
    строки, и наивная команда оставила бы фотографию в бакете навсегда."""

    def test_storage_object_is_actually_gone(self, user) -> None:
        scan = _scan(user, age_days=40)
        storage, name = scan.image.storage, scan.image.name

        assert storage.exists(name), "стража: файл действительно лежал"

        _run("--apply")

        assert not FoodScan.objects.filter(pk=scan.pk).exists()
        assert not storage.exists(name), (
            "строка снята, а объект остался в хранилище — это худший исход: "
            "найти файл по базе больше нельзя"
        )

    def test_a_storage_failure_leaves_the_row_alone(self, user, monkeypatch) -> None:
        """Отказ хранилища НЕ снимает строку.

        Иначе потерянная ссылка на живой файл выдавалась бы за успешную
        очистку, и следующий запуск уже ничего бы не нашёл.
        """
        scan = _scan(user, age_days=40)

        from django.core.files.storage import FileSystemStorage

        def _boom(*a, **kw):
            raise OSError("bucket unreachable")

        # Подменяется КЛАСС хранилища, а не `scan.image.storage`: последнее
        # — ленивый прокси `DefaultStorage`, у которого своего `exists` нет.
        monkeypatch.setattr(FileSystemStorage, "exists", _boom)

        output = _run("--apply")

        assert FoodScan.objects.filter(pk=scan.pk).exists(), (
            "строка обязана пережить отказ хранилища"
        )
        assert "ОТКАЗ" in output
        assert "bucket unreachable" in output, "причина обязательна"
        assert "НЕ УДАЛЕНО В СРОК (§134): 1" in output


class TestAbsentObjectIsNotSuccess:
    def test_row_without_object_is_counted_apart(self, user) -> None:
        """На пилоте пятнадцать строк ссылаются на файлы, которых в
        текущем бакете нет: переезд 03.09, самая старая строка 05.05.

        Сложить их с настоящими удалениями значило бы отчитаться об
        уничтожении фотографий, которых команда не видела.
        """
        real = _scan(user, age_days=40)
        ghost = _scan(user, age_days=40)
        ghost.image.storage.delete(ghost.image.name)

        output = _run("--apply")

        assert "Удалено (объект + строка): 1" in output
        assert "Строка снята, объекта нет в ЭТОМ хранилище: 1" in output
        # Обе строки сняты — исход отличается отчётом, а не действием.
        assert not FoodScan.objects.filter(pk__in=[real.pk, ghost.pk]).exists()

    def test_absence_here_is_not_called_proof_of_deletion(self, user) -> None:
        """«Объекта нет» значит «нет ЗДЕСЬ», и отчёт обязан это сказать.

        Пилот переехал 03.09; объекты прежних строк могли остаться в
        MinIO брошенной машины. Отличить «удалён» от «лежит в другом
        месте» изнутри нельзя — значит нельзя и молчать об этом.
        """
        ghost = _scan(user, age_days=40)
        ghost.image.storage.delete(ghost.image.name)

        output = _run("--apply")

        assert "НЕ доказательство удаления" in output

    def test_a_row_that_never_had_a_photo_is_counted_apart(self, user) -> None:
        """Пятый исход. «Фотография была и делась» и «её не было никогда»
        — разные утверждения; слитые, они сообщали бы об исчезновении
        снимков, которых не существовало."""
        bare = _scan(user, age_days=40, with_image=False)

        output = _run("--apply")

        assert "Строка снята, фотографии не было вовсе: 1" in output
        assert "Строка снята, объекта нет в ЭТОМ хранилище: 0" in output
        assert not FoodScan.objects.filter(pk=bare.pk).exists()


class TestTheClockAndTheDryRun:
    def test_the_same_row_is_kept_young_and_purged_old(self, user) -> None:
        """Положительная стража ко всем «не удалилось».

        Без неё «молодое не трогается» зеленело бы и у команды, которая
        не удаляет НИЧЕГО. Здесь одна и та же строка проверяется дважды,
        и различает их только возраст.
        """
        scan = _scan(user, age_days=29)

        _run("--apply")
        assert FoodScan.objects.filter(pk=scan.pk).exists(), (
            "двадцать девять суток — срок не вышел"
        )

        FoodScan.objects.filter(pk=scan.pk).update(
            created_at=timezone.now() - timedelta(days=31)
        )
        _run("--apply")
        assert not FoodScan.objects.filter(pk=scan.pk).exists(), (
            "та же строка, состаренная, обязана уйти"
        )

    def test_dry_run_changes_nothing(self, user) -> None:
        scan = _scan(user, age_days=40)
        name = scan.image.name

        output = _run()

        assert "СУХОЙ ПРОГОН" in output
        assert FoodScan.objects.filter(pk=scan.pk).exists()
        assert scan.image.storage.exists(name), "сухой прогон не трогает хранилище"

    def test_created_at_does_not_move_on_save(self, user) -> None:
        """§134 запрещает продлевать срок при повторном просмотре.

        Сегодня это свойство ``auto_now_add``, то есть везение схемы.
        Тест превращает везение в требование: если кто-то заменит поле на
        ``auto_now``, просроченная фотография станет вечно молодой.
        """
        scan = _scan(user, age_days=40)
        before = scan.created_at

        scan.dish_name = "Солянка"
        scan.save()
        scan.refresh_from_db()

        assert scan.created_at == before


class TestTheDiaryEntrySurvives:
    def test_food_log_keeps_its_numbers(self, user) -> None:
        """§135: остаётся подтверждённая структурированная запись.

        Положительная стража: без неё «просроченных фотографий нет» было
        бы зелёным и у команды, стирающей дневник целиком.
        """
        scan = _scan(user, age_days=40)
        log = FoodLog.objects.create(
            user=user,
            scan=scan,
            dish_name="Борщ",
            calories=320.0,
            logged_at=timezone.now(),
        )

        _run("--apply")

        log.refresh_from_db()
        assert log.calories == 320.0
        assert log.dish_name == "Борщ"
        assert log.scan_id is None, "ссылка обнулилась, запись цела"

    def test_raw_response_goes_with_the_row(self, user) -> None:
        """§135: сырой ответ живёт ровно столько же и уходит вместе."""
        scan = _scan(user, age_days=40, raw={"vision": "описание фотографии словами"})

        assert scan.raw_response, "стража: он действительно был"

        _run("--apply")

        assert not FoodScan.objects.filter(pk=scan.pk).exists()


class TestOrphansAreNamedNotDeleted:
    def test_an_orphan_is_listed_and_left_alone(self, user) -> None:
        """Объект без строки — решение владельца, а не команды.

        На пилоте таких семь. Удалить их молча значило бы решить за
        владельца судьбу данных неизвестного происхождения.
        """
        donor = _scan(user, age_days=1)
        storage, key = donor.image.storage, donor.image.name
        # Строку убираем в обход команды — остаётся объект без ссылки.
        FoodScan.objects.filter(pk=donor.pk).delete()

        assert storage.exists(key), "стража: сирота действительно лежит"

        output = _run("--scan-orphans", "--apply")

        assert "Объектов без строки (сироты): 1" in output
        assert key in output, "сироту надо назвать поимённо, а не счётчиком"
        assert storage.exists(key), "сирота обязан остаться нетронутым"
        assert "НЕ УДАЛЕНО В СРОК (§134): 1" in output

    def test_silence_about_orphans_is_not_zero(self, user) -> None:
        """Без флага число сирот НЕИЗВЕСТНО, и отчёт обязан сказать это.

        Молчание, прочитанное как ноль, — тот же дефект, против которого
        заведена вся задача, только этажом выше.
        """
        _scan(user, age_days=40)

        output = _run("--apply")

        assert "НЕИЗВЕСТНО" in output
        assert "Объектов без строки" not in output
