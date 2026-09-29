"""DRF-2674 — повторный сид вариантов цели не включает обратно выключенное владельцем.

До правки ``seed_goal_options`` делал ``update_or_create`` с ``is_active=True``:
вариант, выключенный владельцем в админке (``is_active`` — ``list_editable``),
повтор сида включал обратно. Докстринг обещал «Deactivated … NOT removed by
reseeding» — и удаления правда не было; поэтому узел «строка не удалена» здесь
НЕ берётся: он зелёный при живом дефекте и создавал ощущение, что обещание
выполняется. Форма — та же, что у DRF-2663 (#611): существующее не трогать.

* k1 — выключенный владельцем вариант после повтора выключен; пара — новый
  вариант заводится активным;
* k2 — подпись и порядок, правленные владельцем, живут; пара — у нового
  варианта подпись и порядок из файла;
* k3 — отчёт называет, сколько вариантов оставлено как есть.
"""
from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from services.models import GoalOption, ServiceCategory

pytestmark = pytest.mark.django_db

CATEGORY = "Базовый ручной массаж"
ROWS = [
    {"key": "relax", "label": "Расслабиться и снять стресс", "sort_order": 10,
     "categories": [CATEGORY]},
]


@pytest.fixture
def seed_file(tmp_path) -> str:
    ServiceCategory.objects.create(name=CATEGORY)
    path = tmp_path / "goal_options.json"
    path.write_text(json.dumps(ROWS, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _seed(seed_file: str) -> str:
    out = StringIO()
    call_command("seed_goal_options", "--file", seed_file, stdout=out)
    return out.getvalue()


def _relax() -> GoalOption:
    return GoalOption.objects.get(key="relax")


class TestK1OwnerDeactivationSurvives:
    def test_a_new_option_is_created_active(self, seed_file):
        _seed(seed_file)

        assert _relax().is_active is True

    def test_an_option_the_owner_switched_off_stays_off(self, seed_file):
        _seed(seed_file)
        GoalOption.objects.filter(key="relax").update(is_active=False)  # владелец в админке

        _seed(seed_file)

        assert _relax().is_active is False, "сид включил обратно выключенный владельцем вариант"


class TestK2OwnerLabelAndOrderSurvive:
    def test_a_new_option_takes_label_and_order_from_the_file(self, seed_file):
        _seed(seed_file)

        option = _relax()
        assert (option.label, option.sort_order) == ("Расслабиться и снять стресс", 10)

    def test_label_and_order_edited_by_the_owner_are_kept(self, seed_file):
        _seed(seed_file)
        GoalOption.objects.filter(key="relax").update(label="Снять стресс", sort_order=3)

        _seed(seed_file)

        option = _relax()
        assert (option.label, option.sort_order) == ("Снять стресс", 3)


class TestK3TheReportNamesWhatWasLeft:
    def test_second_run_reports_one_kept_and_none_created(self, seed_file):
        first = _seed(seed_file)
        second = _seed(seed_file)

        assert "+1 options" in first and "left as is: 0" in first
        assert "+0 options" in second and "left as is: 1" in second
