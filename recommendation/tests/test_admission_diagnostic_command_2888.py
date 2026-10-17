"""DRF-2888 — команда диагностического прогона допуска.

Решение владельца 07.10: подбор проверяется без проверок допуска, с каждой
отдельно и со всеми вместе; результаты — только для тестирования.

Что заперто:

- прогонов ровно десять: ни одной проверки, восемь по одной, все;
- каждый прогон начинается словом «ДИАГНОСТИКА» и называет включённые
  проверки — вывод без шапки потом не отличить от настоящего;
- команда ничего не пишет;
- набор проверок резолверу передаёт только она: ручка и полка политику
  стадий не собирают, поэтому отключить проверку запросом нельзя;
- числа в отчёте — те же, что даёт резолвер на настоящем источнике.
"""
from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from recommendation._admission import ALL_CHECKS, AdmissionCheck
from recommendation.management.commands import recommendation_admission_diagnostic as command
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    _does,
    _master,
    _offer,
    _peel,
    category,
    curator,
    tenant,
)

REPO = Path(__file__).resolve().parents[2]


def _run(*args: str) -> str:
    out = io.StringIO()
    call_command("recommendation_admission_diagnostic", *args, stdout=out)
    return out.getvalue()


def _run_blocks(text: str) -> list[list[str]]:
    """Блоки прогонов: от строки «ДИАГНОСТИКА · прогон:» до следующей такой или до конца."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith(f"{command.HEADER} · прогон:"):
            blocks.append([line])
        elif line.startswith(f"{command.HEADER} · по кандидатам"):
            break
        elif blocks:
            blocks[-1].append(line)
    return blocks


class TestTheSetsOfChecks:
    def test_none_then_each_alone_then_all(self):
        sets = [checks for _, checks in command.runs()]

        assert sets[0] == frozenset()
        assert sets[1:-1] == [frozenset({check}) for check in ALL_CHECKS]
        assert sets[-1] == frozenset(ALL_CHECKS)
        assert len(sets) == 10

    def test_every_check_has_a_human_title(self):
        assert set(command.TITLES) == set(AdmissionCheck)


@pytest.mark.django_db
class TestTheReport:
    @pytest.fixture
    def pool(self, tenant, category, curator):  # noqa: F811
        """Стрижка (открыта) и пилинг без лицензии (закрыт проверкой лицензии)."""
        _does(_master(tenant, "91"), _offer(tenant, category, curator, name="Стрижка"))
        _does(_master(tenant, "92"), _peel(tenant, category, curator))
        return tenant

    def test_every_run_is_headed_as_a_diagnostic_and_names_its_checks(self, pool):
        blocks = _run_blocks(_run())

        assert len(blocks) == 10
        for block in blocks:
            assert block[0].startswith("ДИАГНОСТИКА · прогон:")
            assert block[1].strip().startswith("включены:")
        assert "— ни одной —" in blocks[0][1]
        assert all(check.value in blocks[-1][1] for check in ALL_CHECKS)

    def test_the_report_opens_with_what_was_measured(self, pool):
        head = _run().splitlines()[:8]

        assert head[0].startswith("ДИАГНОСТИКА") and "только чтение" in head[0]
        assert any(line.startswith("флаг BODY_CARE_UNCLASSIFIED_FAIL_CLOSED: выключен") for line in head)
        assert any(line.startswith("салоны: весь каталог") for line in head)
        assert any("обычный клиент" in line for line in head)

    def test_the_numbers_are_what_the_resolver_gives(self, pool):
        blocks = _run_blocks(_run())

        def shown(block) -> tuple[int, int]:
            line = next(row for row in block if "в выдаче:" in row)
            numbers = [int(part) for part in line.replace("·", " ").split() if part.isdigit()]
            return numbers[0], numbers[1]

        assert shown(blocks[0]) == (2, 0), "без проверок допуска проходят оба"
        assert shown(blocks[-1]) == (1, 1), "со всеми — пилинг закрыт"
        licence = next(b for b in blocks if command.TITLES[AdmissionCheck.LICENSE] in b[0])
        assert shown(licence) == (1, 1)
        assert any("ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED" in row for row in licence)
        mapping_only = next(b for b in blocks if command.TITLES[AdmissionCheck.MAPPING] in b[0])
        assert shown(mapping_only) == (2, 0), "связь у обоих подтверждена — одна эта проверка никого не закрывает"

    def test_the_matrix_counts_each_check_separately(self, pool):
        text = _run()

        licence_row = next(line for line in text.splitlines() if line.strip().startswith("лицензия салона"))
        counts = [int(part) for part in licence_row.split() if part.isdigit()]
        # пройдена · не пройдена · не определено · НЕ ДЕЙСТВУЕТ · не относится
        assert counts[1] == 1, "пилинг: лицензия не пройдена"
        assert sum(counts) == 2

    def test_a_tenant_filter_narrows_the_scope_and_an_unknown_slug_is_refused(self, pool):
        assert f"салоны: {pool.slug}" in _run("--tenant", pool.slug)
        with pytest.raises(CommandError):
            _run("--tenant", "no-such-salon")

    def test_details_list_candidates_by_id_without_names(self, pool):
        text = _run("--details")

        assert "ДИАГНОСТИКА · по кандидатам" in text
        assert "Мастер 91" not in text and "Мастер 92" not in text

    def test_the_command_writes_nothing(self, pool):
        with CaptureQueriesContext(connection) as captured:
            _run("--details")

        writes = [
            q["sql"] for q in captured
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP"))
        ]
        assert captured, "контроль: запросы перехватываются"
        assert writes == []


class TestOnlyTheDiagnosticBuildsItsOwnPolicy:
    """Отключить проверку допуска запросом нельзя по построению."""

    ALLOWED = {"recommendation/management/commands/recommendation_admission_diagnostic.py"}

    def _callers(self) -> dict[str, list[int]]:
        found: dict[str, list[int]] = {}
        for path in REPO.rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if "/tests/" in rel or "/migrations/" in rel or rel.startswith(("venv/", ".venv/")):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "resolve" and any(kw.arg == "policy" for kw in node.keywords):
                    found.setdefault(rel, []).append(node.lineno)
                if name == "StagePolicy" and any(kw.arg == "admission_checks" for kw in node.keywords):
                    found.setdefault(rel, []).append(node.lineno)
        return found

    def test_nobody_else_passes_a_stage_policy_to_the_resolver(self):
        callers = self._callers()

        assert set(callers) == self.ALLOWED, f"политику стадий резолверу передаёт кто-то ещё: {callers}"

    def test_the_scan_sees_the_command_itself(self):
        """Контроль: сторож не пуст — он находит единственное разрешённое место."""
        assert len(self._callers()[next(iter(self.ALLOWED))]) >= 2

    def test_the_resolve_endpoint_takes_no_policy(self):
        source = (REPO / "recommendation" / "views.py").read_text(encoding="utf-8")

        assert "StagePolicy" not in source and "admission_checks" not in source
