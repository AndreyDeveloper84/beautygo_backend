"""DRF-2663 — повторный сид канонического каталога не трогает решённое человеком.

До правки ``seed_canonical_catalog`` делал ``update_or_create`` и на повторе:
возвращал флаг гейта здоровья к черновику файла, оставляя рядом «подтвердил
человек» (провенанс DRF-2614); обнулял длительности куратора; переписывал
одобрение человека на одобрение правилом; ставил ``approved_at`` датой КАЖДОГО
прогона — отметка «одобрено» никогда не выглядела старой.

Узлы — парами: у новой строки сид ставит своё (черновой флаг, одобрение
правилом), у существующей — не трогает ничего. Узла «после сида approved_at
не пуст» здесь нет: он зелёный при живом дефекте.

* u1 — флаг гейта, подтверждённый человеком, и противопоказания живут;
* u2 — одобрение человеком: та же дата, тот же автор; одобрение правилом не
  освежается датой прогона;
* u3 — длительности, ``is_popular``, ``sort_order`` куратора живут;
* u4 — черновой (``PROVISIONAL``) канон сид не одобряет;
* u5 — отчёт называет, сколько строк оставлено как есть.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from services.models import ServiceTemplate

User = get_user_model()
L = ServiceTemplate.Lifecycle
HC = ServiceTemplate.HealthCheckOrigin

pytestmark = pytest.mark.django_db

NAME = "Ботулотоксин лба"
SAMPLE = [
    {
        "code": "6.1.1", "category_no": 6, "category": "Инъекционная косметология",
        "subcategory_no": "6.1", "subcategory": "Ботулинотерапия",
        "service": NAME, "note": "только медицинский профиль",
        "requires_health_check": "true", "health_check_reason": "category-medical",
    },
]
PAST = datetime(2026, 9, 1, 10, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def seed_file(tmp_path) -> str:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def human():
    return User.objects.create_user(username="curator-2663", password="x")


def _seed(seed_file: str) -> str:
    out = StringIO()
    call_command("seed_canonical_catalog", "--file", seed_file, stdout=out)
    return out.getvalue()


def _row() -> ServiceTemplate:
    return ServiceTemplate.objects.get(name=NAME)


def _rows() -> "QuerySet[ServiceTemplate]":  # noqa: F821
    return ServiceTemplate.objects.filter(name=NAME)


class TestU1HealthGateConfirmedByAHumanSurvives:
    def test_a_new_row_gets_the_files_draft_flag(self, seed_file):
        _seed(seed_file)

        row = _row()
        assert row.requires_health_check is True
        assert row.health_check_origin == HC.INFERRED
        assert row.contraindications == "только медицинский профиль"

    def test_a_flag_a_human_confirmed_is_not_reset_to_the_draft(self, seed_file, human):
        _seed(seed_file)
        _rows().update(
            requires_health_check=False,
            contraindications="допущено врачом",
            health_check_origin=HC.CONFIRMED,
            health_check_confirmed_by=human,
            health_check_confirmed_at=PAST,
            health_check_source_ref="разбор владельца",
        )

        _seed(seed_file)

        row = _row()
        assert row.requires_health_check is False, "сид вернул флаг к черновику файла"
        assert row.contraindications == "допущено врачом"
        assert row.health_check_origin == HC.CONFIRMED
        assert row.health_check_confirmed_by_id == human.pk
        assert row.health_check_confirmed_at == PAST


class TestU2ApprovalKeepsItsAuthorAndItsDate:
    def test_a_new_row_is_approved_by_the_rule(self, seed_file):
        _seed(seed_file)

        row = _row()
        assert row.lifecycle == L.APPROVED
        assert row.approved_rule == "seed_canonical_catalog"
        assert row.approved_by_id is None

    def test_a_human_approval_keeps_the_same_date_and_author(self, seed_file, human):
        _seed(seed_file)
        _rows().update(
            approved_by=human, approved_rule="", approval_rule_version="",
            approved_at=PAST, approval_source_ref="реестр владельца",
        )

        _seed(seed_file)

        row = _row()
        assert row.approved_at == PAST, "дата одобрения человеком сдвинута прогоном"
        assert row.approved_by_id == human.pk, "автор одобрения стёрт"
        assert row.approved_rule == ""

    def test_a_rule_approval_is_not_refreshed_by_the_next_run(self, seed_file):
        # Сама находка DRF-2663: дата «одобрено» освежалась каждым прогоном.
        _seed(seed_file)
        _rows().update(approved_at=PAST)  # одобрено правилом месяц назад

        _seed(seed_file)

        assert _row().approved_at == PAST


class TestU3CuratorFieldsSurvive:
    def test_durations_popularity_and_order_are_kept(self, seed_file):
        _seed(seed_file)
        assert _row().duration_default is None  # пара: у новой строки пусто
        _rows().update(
            duration_default=60, duration_min=45, duration_max=90,
            is_popular=True, sort_order=99,
        )

        _seed(seed_file)

        row = _row()
        assert (row.duration_default, row.duration_min, row.duration_max) == (60, 45, 90)
        assert row.is_popular is True
        assert row.sort_order == 99


class TestU4ProvisionalIsNotApprovedByTheSeed:
    def test_a_canon_returned_to_draft_stays_draft(self, seed_file):
        _seed(seed_file)
        _rows().update(
            lifecycle=L.PROVISIONAL, approved_rule="", approval_rule_version="",
            approved_at=None, approval_source_ref="",
        )

        _seed(seed_file)

        assert _row().lifecycle == L.PROVISIONAL


class TestU5TheReportNamesWhatWasLeft:
    def test_second_run_reports_one_kept_and_none_created(self, seed_file):
        first = _seed(seed_file)
        second = _seed(seed_file)

        assert "+1 templates" in first and "left as is: 0" in first
        assert "+0 templates" in second and "left as is: 1" in second
