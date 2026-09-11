"""Снимок базы каталога стоит ПЕРЕД всем, что меняет схему — DRF-1664, B-5 (5.1).

У каталога нет выкладки по слиянию. Схему пилота меняют два пути, и оба
делали это без точки отката:

* ``deploy.sh`` (руками, runbook §1.1) — ``docker compose up -d``, а entrypoint
  web-контейнера сам делает ``migrate`` при старте;
* ``.github/workflows/smoke-on-dev.yml`` (каждое утро 04:00 UTC) — явный
  ``migrate`` после ``git reset --hard origin/dev``.

Что держат эти тесты: снимок зовётся в ОБОИХ и стоит ДО шага, меняющего
схему; сам скрипт отказывает на пустом дампе, не пишет внутрь дерева,
ротирует и называет актора числом; процесс в контейнере несёт ``--user``
(кто позвал клиента docker и кто работает внутри — разные субъекты).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "pg_snapshot_before_deploy.sh"
DEPLOY = REPO / "deploy.sh"
SMOKE = REPO / ".github" / "workflows" / "smoke-on-dev.yml"


def _code(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _index(lines: list[str], needle: str) -> int:
    return next((i for i, ln in enumerate(lines) if needle in ln), -1)


def test_the_script_exists_and_parses() -> None:
    """Присутствие впереди всего: без файла остальные тесты нашли бы «ноль
    нарушений» в пустоте."""

    assert SCRIPT.is_file(), f"{SCRIPT} не найден"
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash недоступен — синтаксис проверит CI")
    proc = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_deploy_sh_snapshots_before_the_containers_come_up() -> None:
    lines = _code(DEPLOY.read_text(encoding="utf-8"))
    snap = _index(lines, "pg_snapshot_before_deploy.sh")
    up = _index(lines, "docker compose up -d")

    assert up != -1, "deploy.sh перестал поднимать контейнеры — проверять нечего"
    assert snap != -1, "deploy.sh не делает снимок — up -d мигрирует схему без точки отката"
    assert snap < up, f"снимок ({snap}) стоит ПОСЛЕ up -d ({up}) — entrypoint уже смигрировал"


def test_the_smoke_workflow_snapshots_before_migrate() -> None:
    doc = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
    steps = doc["jobs"]["smoke"]["steps"]
    host = next((s for s in steps if "pg_snapshot_before_deploy.sh" in str(s.get("with", {}))), None)
    assert host is not None, "smoke-on-dev не зовёт снимок — утренний migrate идёт без точки отката"

    lines = _code(host["with"]["script"])
    snap = _index(lines, "pg_snapshot_before_deploy.sh")
    migrate = _index(lines, "manage.py migrate")
    assert migrate != -1, "шаг перестал мигрировать — проверять нечего"
    assert snap < migrate, f"снимок ({snap}) стоит ПОСЛЕ migrate ({migrate})"


class TestTheScriptItself:
    lines = _code(SCRIPT.read_text(encoding="utf-8")) if SCRIPT.is_file() else []

    def test_the_container_process_carries_user_from_stat(self) -> None:
        """``--user`` у exec, и берётся из ``stat`` дерева, а не из ``id -u``
        зовущего: прокси «зовущий = владелец» под root даёт 0:0 (DRF-1646)."""

        dump = [ln for ln in self.lines if "pg_dump" in ln or "exec -T" in ln]
        assert dump, "строки exec/pg_dump нет"
        joined = " ".join(dump)
        assert "--user" in joined, "процесс в контейнере пойдёт от uid образа (root)"
        assert any("stat -c %u" in ln for ln in self.lines), "uid берётся не из stat дерева"
        assert "$(id -u)" not in joined, "--user взял зовущего, а не владельца дерева"

    def test_an_empty_dump_is_a_refusal(self) -> None:
        size = _index(self.lines, "stat -c %s")
        refuse = next((i for i, ln in enumerate(self.lines) if "-lt 1024" in ln), -1)
        assert size != -1 and refuse != -1, "нет порога размера — пустой gzip прошёл бы за успех"
        assert any("exit 1" in ln for ln in self.lines[refuse:refuse + 4]), "порог есть, отказа нет"

    def test_the_snapshot_never_lands_inside_the_tree(self) -> None:
        guard = _index(self.lines, "внутри дерева выкладки")
        first_write = _index(self.lines, "mkdir -p")
        assert guard != -1, "нет отказа на снимок внутри дерева"
        assert first_write != -1 and guard < first_write, "проверка вложенности стоит после записи"

    def test_the_write_is_probed_before_the_dump(self) -> None:
        probe = _index(self.lines, ".write-probe")
        dump = _index(self.lines, "pg_dump")
        assert probe != -1 and dump != -1 and probe < dump, "проба записи не стоит до дампа"

    def test_rotation_is_by_name_and_bounded(self) -> None:
        rot = [ln for ln in self.lines if "pre-deploy-*.sql.gz" in ln and "sort -r" in ln]
        assert rot, "ротации нет — снимки растут без предела"
        assert re.search(r"KEEP\s*\+\s*1", rot[0]), "ротация не привязана к KEEP"

    def test_the_actor_is_printed_as_a_number(self) -> None:
        said = [ln for ln in self.lines if "::notice::" in ln and "id -u" in ln]
        assert said, "скрипт не называет, от кого пишет"
        assert all("id -un" not in ln for ln in said), "актор печатается именем — на раннере оно маскируется"
