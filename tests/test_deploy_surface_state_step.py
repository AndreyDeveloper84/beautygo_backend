"""Форма шага публикации состояния поверхности в job `deploy` (DRF-1661, каталожная половина).

Шаг делает три вещи, и каждая имеет способ сломаться так, что прогон останется
зелёным: файл может лечь от root (владелец дерева его не перезапишет), может
лечь в отслеживаемый путь (следующий `git reset --hard` его снесёт или он
попадёт в коммит), может не появиться вовсе (а ::notice:: скажет «записано»).
Поэтому форма шага читается по YAML — не по литералам в тексте файла — и
проверяется по частям.

Класс DRF-1646: `exec -T` без `--user` наследует uid работающего контейнера,
а он до #354 — root. Сторож на это стоит на всех ssh-скриптах job'а, и у него
есть положительный контроль на синтетическом шаге.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
STEP_NAME = "Publish surface state (catalog) into the deploy tree"
OUT_PATH = "docs/generated/SURFACE_STATE.md"


def _deploy_steps() -> list[dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["deploy"]["steps"]


def _script(step: dict) -> str:
    return (step.get("with") or {}).get("script") or step.get("run") or ""


def _code_lines(script: str) -> list[str]:
    """Строки без комментариев: сторож стережёт программу, не документацию."""
    return [ln for ln in script.splitlines() if ln.strip() and not ln.strip().startswith("#")]


@pytest.fixture(scope="module")
def step() -> dict:
    found = [s for s in _deploy_steps() if s.get("name") == STEP_NAME]
    assert len(found) == 1, f"шаг {STEP_NAME!r} не найден или не единственный: {len(found)}"
    return found[0]


# ─── файл кладётся в игнорируемый путь ──────────────────────────────────────


def test_the_output_path_is_git_ignored() -> None:
    """Иначе `git reset --hard` следующей выкладки снесёт файл, либо он попадёт в коммит.

    Спрашивается git, а не .gitignore текстом: правило может стоять в любой из
    форм, и только `check-ignore` знает, какая действует.
    """
    r = subprocess.run(
        ["git", "check-ignore", "-q", OUT_PATH], cwd=REPO, capture_output=True
    )
    assert r.returncode == 0, f"{OUT_PATH} не игнорируется git — он попадёт под reset --hard или в коммит"


def test_the_ignore_check_would_notice_a_tracked_path() -> None:
    """Положительный контроль: путь, который точно отслеживается, check-ignore обязан пропустить."""
    r = subprocess.run(["git", "check-ignore", "-q", "manage.py"], cwd=REPO, capture_output=True)
    assert r.returncode != 0


def test_the_step_writes_exactly_the_ignored_path(step: dict) -> None:
    assert f"OUT={OUT_PATH}" in _script(step)


# ─── процесс в контейнере идёт от владельца дерева ──────────────────────────


def test_the_process_runs_as_the_tree_owner_not_the_caller(step: dict) -> None:
    """`--user` берёт uid у ДЕРЕВА (stat), не у зовущего (`id -u`).

    Под root `$(id -u)` даёт 0:0 — мера, работающая ровно как написана, и не
    делающая ничего (DRF-1646).
    """
    code = "\n".join(_code_lines(_script(step)))
    assert re.search(r"TREE_UID=\$\(stat -c %u \.\)", code), "uid дерева не берётся через stat"
    assert re.search(r'run --rm[^\n]*--user "\$TREE_UID:\$TREE_GID"', code), "compose run без --user владельца дерева"
    assert "$(id -u)" not in code.split("--user")[1].split("\n")[0], "--user через id -u — под root это 0:0"


def test_it_is_run_not_exec(step: dict) -> None:
    """`exec` унаследовал бы uid работающего контейнера — до #354 это root."""
    code = "\n".join(_code_lines(_script(step)))
    assert "docker compose run --rm" in code
    assert "exec -T" not in code


# ─── ::notice:: несёт числа, отказ — при отсутствии файла ───────────────────


def test_the_notice_prints_file_uid_next_to_tree_uid_and_line_count(step: dict) -> None:
    code = "\n".join(_code_lines(_script(step)))
    notice = next((ln for ln in code.splitlines() if "::notice::surface_state" in ln), "")
    assert notice, "нет ::notice:: о записи"
    assert 'stat -c %u "$OUT"' in notice, "uid файла не печатается"
    assert "$TREE_UID" in notice, "uid дерева не печатается рядом"
    assert 'wc -l < "$OUT"' in notice, "число строк не печатается"


def test_a_missing_or_empty_file_fails_the_step(step: dict) -> None:
    code = "\n".join(_code_lines(_script(step)))
    assert 'rm -f "$OUT"' in code, "старый файл не снимается — «записано» могло бы означать вчерашний"
    assert re.search(r'if \[ ! -s "\$OUT" \]; then\s*\n\s*echo "::error::[^\n]*\n\s*exit 1', code), (
        "отсутствие/пустота файла не роняет шаг"
    )


# ─── класс DRF-1646 на всём job'е: exec -T без --user запрещён ──────────────


def _bare_exec_lines(script: str) -> list[str]:
    return [
        ln.strip()
        for ln in _code_lines(script)
        if re.search(r"\bexec -T\b", ln) and "--user" not in ln
    ]


def test_no_deploy_script_execs_into_a_container_without_user() -> None:
    offenders = {
        s.get("name"): _bare_exec_lines(_script(s))
        for s in _deploy_steps()
        if _bare_exec_lines(_script(s))
    }
    assert offenders == {}, f"exec -T без --user наследует uid контейнера (root до #354): {offenders}"


def test_the_exec_guard_would_catch_a_bare_exec() -> None:
    """Положительный контроль: синтетический скрипт с голым exec обязан быть пойман."""
    bad = "docker compose exec -T web python manage.py shell\n"
    good = 'docker compose exec -T --user "$TREE_UID:$TREE_GID" web python manage.py shell\n'
    assert _bare_exec_lines(bad) == [bad.strip()]
    assert _bare_exec_lines(good) == []
    assert _bare_exec_lines("# exec -T web в комментарии\n") == []


# ─── путь записи в контейнере обязан быть смонтирован с хоста ───────────────
#
# Второй лист DRF-1661 (run 34659054414, 00:28 12.09.2026): исходники каталога
# запечены в образ, с хоста смонтированы только staticfiles/ и media/. Без
# монтирования `--write docs/generated/…` шёл в каталог root ВНУТРИ образа
# → EACCES под uid дерева; а даже удавшись, файл не появился бы на хосте.
# Инвариант: контейнерный путь, куда пишет команда, host-backed — либо
# `-v` на самом run, либо compose монтирует /app (как у бота) или сам этот
# каталог. Стережётся по трём файлам: ci.yml, Dockerfile (WORKDIR), compose.

COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile"


def _workdir() -> str:
    lines = [ln.split()[1] for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines() if ln.startswith("WORKDIR ")]
    assert lines, "в Dockerfile нет WORKDIR — контейнерный путь записи неизвестен"
    return lines[-1].rstrip("/")


def _run_line(script: str) -> str:
    """Строка `docker compose run …` со склеенными продолжениями обратной косой."""
    code = _code_lines(script)
    joined: list[str] = []
    for ln in code:
        if joined and joined[-1].endswith("\\"):
            joined[-1] = joined[-1][:-1] + " " + ln.strip()
        else:
            joined.append(ln.strip())
    found = [ln for ln in joined if ln.startswith("docker compose run")]
    assert len(found) == 1, found
    return found[0]


def _run_bind_mounts(run_line: str) -> dict[str, str]:
    """{контейнерный путь: хостовый} из `-v`/`--volume` на строке run."""
    return {
        cont.rstrip("/"): host
        for host, cont in re.findall(r'(?:-v|--volume)\s+"?([^:\s"]+):([^:\s"]+)', run_line)
    }


def _compose_web_mounts() -> dict[str, str]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for vol in data["services"]["web"].get("volumes") or []:
        if isinstance(vol, str) and ":" in vol:
            host, cont = vol.split(":")[:2]
            out[cont.rstrip("/")] = host
    return out


def _write_dir_is_host_backed(run_line: str, compose_mounts: dict[str, str], workdir: str) -> bool:
    cont_dir = f"{workdir}/{OUT_PATH.rsplit('/', 1)[0]}"
    mounts = {**compose_mounts, **_run_bind_mounts(run_line)}
    return any(cont_dir == m or cont_dir.startswith(m + "/") for m in mounts)


def test_the_container_write_dir_is_bind_mounted_from_the_tree(step: dict) -> None:
    run_line = _run_line(_script(step))
    assert _write_dir_is_host_backed(run_line, _compose_web_mounts(), _workdir()), (
        f"{_workdir()}/{OUT_PATH} пишется ВНУТРЬ образа: ни -v на run, ни монтирование в compose. "
        "Под uid дерева это EACCES, а под root файл остался бы в контейнере."
    )


def test_the_run_mount_points_at_the_tree_not_elsewhere(step: dict) -> None:
    """Хостовая сторона `-v` — каталог вывода в дереве (`$PWD/…`), не media/ (nginx отдаёт его наружу)."""
    mounts = _run_bind_mounts(_run_line(_script(step)))
    out_dir = OUT_PATH.rsplit("/", 1)[0]
    host = mounts.get(f"{_workdir()}/{out_dir}")
    assert host == f"$PWD/{out_dir}", mounts
    assert "/media" not in host


def test_the_host_dir_is_created_by_the_ssh_user_before_the_run(step: dict) -> None:
    """Несуществующий источник bind-mount docker создаёт сам — от root; uid дерева туда не запишет."""
    code = _code_lines(_script(step))
    out_dir = OUT_PATH.rsplit("/", 1)[0]
    mk = next((i for i, ln in enumerate(code) if ln.strip() == f"mkdir -p {out_dir}"), None)
    run = next((i for i, ln in enumerate(code) if ln.strip().startswith("docker compose run")), None)
    assert mk is not None and run is not None and mk < run, (code[:12], mk, run)


def test_the_mount_guard_would_notice_an_unmounted_write() -> None:
    """Положительный контроль: та форма шага, что упала 00:28, обязана быть поймана."""
    bare = (
        'docker compose run --rm --no-deps --user "$TREE_UID:$TREE_GID" '
        'web python manage.py surface_state --write "$OUT"'
    )
    only_static = {"/app/staticfiles": "/x/staticfiles", "/app/media": "/x/media"}
    assert not _write_dir_is_host_backed(bare, only_static, "/app")
    with_v = bare.replace("web python", '-v "$PWD/docs/generated:/app/docs/generated" web python')
    assert _write_dir_is_host_backed(with_v, only_static, "/app")
    # как у бота: compose монтирует всё дерево — -v не нужен
    assert _write_dir_is_host_backed(bare, {"/app": "./"}, "/app")
    # смонтирован соседний каталог — не считается
    assert not _write_dir_is_host_backed(bare, {"/app/docs/other": "./docs/other"}, "/app")
