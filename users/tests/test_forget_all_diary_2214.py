"""«Забудь всё» стирает дневник питания — и настоящий путь бота стирает всё (DRF-2214).

# Настоящий путь бота — C5.2, а не ``personal-context``

#526 правил ``DELETE /internal/users/{id}/personal-context/`` и называл его
путём бота. Бот его не зовёт: «забудь всё» бота
(ai-bot-platform ``apps/identity/services/personal_context.py:239`` — «The ONE
erase verb: DELETE /internal/users/{id}/personal-data/») и задание повтора
DRF-1950 (``apps/identity/services/ayla_erasure.py:285``) идут в C5.2 —
``InternalPersonalDataDeleteView``. Здесь URL собран так же, как его собирает
клиент бота (``apps/integrations/ayla/personal_context_client.py`` —
``f"internal/users/{ayla_user_id}/personal-data/"`` от базы ``/api/v1/``): все
узлы пути бота бьют в тот же URL, а ``test_the_bot_erase_url_is_the_c52_view``
держит, что он разрешается в C5.2. Дрейф клиента бота отсюда не виден.

# Дневник — по слову владельца (CURRENT_DECISIONS §66: «а — стирать дневник вместе со всем»)

Текст команды обещает забыть всё, кроме бронирований, оплат, настроек
уведомлений и даты рождения; код приводится к тексту. Дневник — ``FoodLog``,
``FoodScan`` с фото, ``WaterEntry``, ``SavedMeal`` (и мягко удалённые) — и
соседи из того же шага D3: ``DeletedFoodLog`` (снимок на окно восстановления —
без него «восстановить» вернёт стёртое), ``WaterLog`` (старый дневник воды),
``ProfileIdempotencyKey`` (суточный кэш ответа с профилем питания),
``CrossDomainShownRule`` (история показанных подсказок).

# Фото — до коммита, как D3

Файл снимается раньше строк внутри транзакции. Откат оставляет строку без
файла — повтор найдёт строку и дочистит; файл без строки (``on_commit``,
упавший после коммита) не нашёл бы никто. Узел
``test_a_rollback_after_the_photo_left_keeps_the_row_not_the_photo`` держит это.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import resolve
from django.utils import timezone

from nutrition.models import (
    CrossDomainRule,
    CrossDomainShownRule,
    DeletedFoodLog,
    FoodLog,
    FoodScan,
    ProfileIdempotencyKey,
    SavedMeal,
    WaterEntry,
    WaterLog,
)
from users.deletion_executor import IncompleteErasure
from users.models import UserPersonalContext
from users.personal_data_api import InternalPersonalDataDeleteView
from users.tests.test_forget_all_catalog_2214 import (
    _remembered_counts,
    _seed_remembered,
)
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    APP_PC_URL,
    _app,
    _internal,
    _set_token,
    _url,
    user,
)

User = get_user_model()
pytestmark = pytest.mark.django_db

#: Как клиент бота: база ``/api/v1/`` + ``f"internal/users/{ayla_user_id}/personal-data/"``
#: (ai-bot-platform ``apps/integrations/ayla/personal_context_client.py::delete_personal_data``;
#: единственный глагол стирания — ``apps/identity/services/personal_context.py:239``).
BOT_ERASE_URL = "/api/v1/" + "internal/users/{ayla_user_id}/personal-data/"


def _bot_url(u) -> str:
    return BOT_ERASE_URL.format(ayla_user_id=u.pk)


def _forget_via_bot(u) -> None:
    resp = _internal().delete(_bot_url(u))
    assert resp.status_code == 200, resp.content


def _forget_via_app(u) -> None:
    resp = _app(u).delete(APP_PC_URL)
    assert resp.status_code == 204, resp.content


def _forget_via_personal_context(u) -> None:
    resp = _internal().delete(_url(u.id))
    assert resp.status_code == 200, resp.content


ALL_PATHS = pytest.mark.parametrize(
    "forget",
    [_forget_via_bot, _forget_via_app, _forget_via_personal_context],
    ids=["бот-C5.2", "приложение", "personal-context"],
)

#: Путь → модуль, где вид зовёт ``erase_personal_context``.
REAL_PATHS_WITH_MODULE = pytest.mark.parametrize(
    "forget, view_module",
    [
        # DRF-2305 — оба пути зовут общий глагол ``users.forget_all_subject``.
        (_forget_via_bot, "users.forget_all_subject"),
        (_forget_via_app, "users.forget_all_subject"),
    ],
    ids=["бот-C5.2", "приложение"],
)


def _seed_diary(u, tag: str = "") -> FoodScan:
    now = timezone.now()
    scan = FoodScan.objects.create(user=u, dish_name="Борщ", provider_used=FoodScan.Provider.OPENAI)
    scan.image.save(f"scan2214{tag}.jpg", ContentFile(b"s" * 10), save=True)
    log = FoodLog.objects.create(
        user=u, scan=scan, dish_name="Борщ", meal_type="lunch", logged_at=now,
        idempotency_key=str(uuid.uuid4()),
    )
    DeletedFoodLog.objects.create(
        id=uuid.uuid4(), user=u, snapshot={"dish_name": "Окрошка"}, deleted_at=now
    )
    WaterEntry.objects.create(user=u, ts=now, ml=250, water_ml=250.0, food_log=log)
    WaterEntry.objects.create(user=u, ts=now, ml=200, water_ml=200.0, deleted_at=now)
    WaterLog.objects.create(user=u, amount_ml=250, logged_at=now)
    rule, _ = CrossDomainRule.objects.get_or_create(
        rule_id="fa2214_rule",
        defaults=dict(
            nutrition_trigger="low_vitamin_d", service_category_slug="massage",
            insight_text_template="t", rationale_text="r", disclaimer_text="d",
            is_active=True, legal_reviewed=True,
        ),
    )
    CrossDomainShownRule.objects.create(
        user=u, rule=rule, nutrition_trigger="low_vitamin_d",
        service_category_slug="massage", shown_at=now, surface="bot",
    )
    ProfileIdempotencyKey.objects.create(
        key=f"fa2214-{uuid.uuid4()}", user=u, response={"weight_kg": 60},
        expires_at=now + timedelta(hours=1),
    )
    SavedMeal.objects.create(
        user=u, dish_name="борщ", portion_g=250.0, calories=125.0, source_food_log=log
    )
    SavedMeal.objects.create(
        user=u, dish_name="омлет", portion_g=150.0, calories=230.0, deleted_at=now
    )
    return scan


def _diary_counts(u) -> dict[str, int]:
    return {
        "FoodLog": FoodLog.objects.filter(user=u).count(),
        "FoodScan": FoodScan.objects.filter(user=u).count(),
        "DeletedFoodLog": DeletedFoodLog.objects.filter(user=u).count(),
        "WaterEntry": WaterEntry.objects.filter(user=u).count(),
        "WaterLog": WaterLog.objects.filter(user=u).count(),
        "CrossDomainShownRule": CrossDomainShownRule.objects.filter(user=u).count(),
        "ProfileIdempotencyKey": ProfileIdempotencyKey.objects.filter(user=u).count(),
        "SavedMeal": SavedMeal.objects.filter(user=u).count(),
    }


def _photo_exists(scan: FoodScan) -> bool:
    return scan.image.storage.exists(scan.image.name)


def _all_present(counts: dict[str, int]) -> bool:
    return all(n >= 1 for n in counts.values())


def _all_zero(counts: dict[str, int]) -> bool:
    return all(n == 0 for n in counts.values())


@pytest.fixture
def diary(user):  # noqa: F811 — фикстура по имени
    scan = _seed_diary(user)
    UserPersonalContext.objects.create(user=user, diet_type="keto")
    return user, scan


class TestTheBotsRealPath:
    def test_the_bot_erase_url_is_the_c52_view(self, user) -> None:  # noqa: F811
        """URL клиента бота разрешается в C5.2, а не в ``personal-context``.

        Сторож, не красный узел: ловит смену URLconf каталога. Дрейф самого
        клиента бота он не видит — URL здесь копия его f-строки со ссылкой.
        """
        match = resolve(_bot_url(user))
        assert match.func.view_class is InternalPersonalDataDeleteView

    def test_c52_erases_what_was_remembered(self, user) -> None:  # noqa: F811
        """Дыра #526: цели, анкета, план и профиль питания — по настоящему пути бота."""
        _seed_remembered(user)
        before = _remembered_counts(user)
        assert _all_present(before), before

        _forget_via_bot(user)

        after = _remembered_counts(user)
        assert _all_zero(after), after


class TestTheDiaryIsForgotten:
    @ALL_PATHS
    def test_every_diary_store_is_empty(self, diary, forget) -> None:
        u, _ = diary
        before = _diary_counts(u)
        assert _all_present(before), before

        forget(u)

        after = _diary_counts(u)
        assert _all_zero(after), after

    @ALL_PATHS
    def test_the_scan_photo_leaves_storage(self, diary, forget) -> None:
        """Не только строка: сам файл снят с носителя."""
        u, scan = diary
        assert _photo_exists(scan)

        forget(u)

        assert not _photo_exists(scan)

    @ALL_PATHS
    def test_a_second_forget_is_harmless(self, diary, forget) -> None:
        u, scan = diary
        before = _diary_counts(u)
        assert _all_present(before), before
        assert _photo_exists(scan)

        forget(u)
        forget(u)  # файла уже нет — не падает

        after = _diary_counts(u)
        assert _all_zero(after), after
        assert not _photo_exists(scan)


class TestTheWholeSubject:
    def test_a_linked_proxys_diary_and_goals_go_via_c52(self, user) -> None:  # noqa: F811
        """Как D3: строки, записанные на прокси до привязки, — тоже этот человек."""
        proxy = User.objects.create(
            username="bot:max:fa2214-proxy", role="client", is_proxy=True, linked_user=user
        )
        scan = _seed_diary(proxy, tag="p")
        _seed_remembered(proxy)
        assert _all_present(_diary_counts(proxy))
        assert _all_present(_remembered_counts(proxy))
        assert _photo_exists(scan)

        _forget_via_bot(user)

        assert _all_zero(_diary_counts(proxy)), _diary_counts(proxy)
        assert _all_zero(_remembered_counts(proxy)), _remembered_counts(proxy)
        assert not _photo_exists(scan)

    def test_a_proxy_without_a_profile_row_gets_no_tombstone(self, user) -> None:  # noqa: F811
        """Дневник прокси стёрт, а надгробие профиля ему не создано (DRF-1038)."""
        proxy = User.objects.create(
            username="bot:max:fa2214-bare", role="client", is_proxy=True, linked_user=user
        )
        _seed_diary(proxy, tag="b")
        assert _all_present(_diary_counts(proxy))
        assert not UserPersonalContext.objects.filter(user=proxy).exists()

        _forget_via_bot(user)

        assert _all_zero(_diary_counts(proxy)), _diary_counts(proxy)
        assert not UserPersonalContext.objects.filter(user=proxy).exists()

    def test_a_neighbour_and_an_unlinked_proxy_are_untouched(self, diary) -> None:
        """Положительная пара: стирается только этот субъект — и его фото, не чужое."""
        u, _ = diary
        neighbour = User.objects.create_user(
            username="fa2214d_neighbour", password="x", role="client", phone="+79995559003"
        )
        stranger = User.objects.create(
            username="bot:max:fa2214-stranger", role="client", is_proxy=True
        )
        n_scan = _seed_diary(neighbour, tag="n")
        s_scan = _seed_diary(stranger, tag="s")
        n_before, s_before = _diary_counts(neighbour), _diary_counts(stranger)
        assert _all_present(n_before) and _all_present(s_before)

        _forget_via_bot(u)

        assert _all_zero(_diary_counts(u)), _diary_counts(u)
        assert _diary_counts(neighbour) == n_before
        assert _diary_counts(stranger) == s_before
        assert _photo_exists(n_scan) and _photo_exists(s_scan)


class TestAllOrNothing:
    @REAL_PATHS_WITH_MODULE
    def test_a_failure_on_the_diary_keeps_everything(self, diary, forget, view_module) -> None:
        """Упало на дневнике (фото не снялось) — не стёрто ничего: ни цели, ни профиль."""
        u, scan = diary
        _seed_remembered(u)
        remembered_before, diary_before = _remembered_counts(u), _diary_counts(u)
        assert _all_present(remembered_before) and _all_present(diary_before)

        assert _photo_exists(scan)  # заодно разворачивает ленивый DefaultStorage
        storage = scan.image.storage
        storage_cls = type(getattr(storage, "_wrapped", storage))
        with mock.patch.object(storage_cls, "delete", side_effect=OSError("disk")):
            with pytest.raises(OSError):
                forget(u)

        assert _remembered_counts(u) == remembered_before
        assert _diary_counts(u) == diary_before
        assert UserPersonalContext.objects.get(user=u).diet_type == "keto"
        assert _photo_exists(scan)

    @REAL_PATHS_WITH_MODULE
    def test_a_rollback_after_the_photo_left_keeps_the_row_not_the_photo(
        self, diary, forget, view_module
    ) -> None:
        """Фото — до коммита (как D3): откат оставляет строку без фото, а не фото без строки.

        Строку повтор найдёт и дочистит; файл без строки не нашёл бы никто.
        """
        u, scan = diary
        assert _photo_exists(scan)
        assert FoodScan.objects.filter(pk=scan.pk).exists()

        with mock.patch(f"{view_module}.erase_personal_context", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                forget(u)

        assert FoodScan.objects.filter(pk=scan.pk).exists()
        assert not _photo_exists(scan)

        forget(u)  # повтор дочищает строку

        assert not FoodScan.objects.filter(pk=scan.pk).exists()

    def test_a_photo_that_stays_on_storage_is_a_500_and_nothing_is_erased(self, diary) -> None:
        """Файл не снялся (носитель сказал «удалён», а он на месте) — честный 500, не «стёрто»."""
        u, scan = diary
        _seed_remembered(u)
        remembered_before, diary_before = _remembered_counts(u), _diary_counts(u)
        assert _all_present(remembered_before) and _all_present(diary_before)
        assert _photo_exists(scan)

        storage = scan.image.storage
        storage_cls = type(getattr(storage, "_wrapped", storage))
        client = _internal()
        client.raise_request_exception = False
        with mock.patch.object(storage_cls, "delete", return_value=None):
            resp = client.delete(_bot_url(u))

        assert resp.status_code == 500
        assert _remembered_counts(u) == remembered_before
        assert _diary_counts(u) == diary_before
        assert UserPersonalContext.objects.get(user=u).diet_type == "keto"
        assert _photo_exists(scan)

    def test_incomplete_erasure_is_what_a_stuck_photo_raises(self, diary) -> None:
        """Пара к узлу выше: 500 — именно от ``IncompleteErasure``, а не от чего попало."""
        u, scan = diary
        assert _photo_exists(scan)

        storage = scan.image.storage
        storage_cls = type(getattr(storage, "_wrapped", storage))
        with mock.patch.object(storage_cls, "delete", return_value=None):
            with pytest.raises(IncompleteErasure):
                _forget_via_bot(u)

        assert _photo_exists(scan)
