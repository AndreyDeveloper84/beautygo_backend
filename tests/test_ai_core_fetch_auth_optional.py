"""Страж: ``GH_DEPLOY_TOKEN`` необязателен для установки ``ayla-ai-core``.

``AndreyDeveloper84/ayla-ai-core`` — публичный репозиторий (решение владельца
от 04.09.2026, ``docs/OPEN_DECISIONS.md`` §22; проверено анонимным запросом,
``"private": false``). Закреплённый в ``requirements.txt`` SHA клонируется
без учётных данных вовсе.

До DRF-1466 шаг ``Configure git auth`` в ``.github/workflows/ci.yml``
переписывал git-URL БЕЗУСЛОВНО. При пустом секрете в глобальный конфиг
уходило ``https://@github.com/`` — URL, собранный из значения, которое шаг
ни разу не посмотрел. Против публичного репозитория это случайно работает
(PR'ы Dependabot #282-284 прошли именно так), против приватного —
вырождается в запрос учётных данных вместо честной ошибки.

Проверка ставится обеими половинами и исполнением, а не чтением:

* без токена — код возврата 0, переписывание URL НЕ записано;
* с токеном — код возврата 0, переписывание записано, значение токена в
  вывод не попало.

Вторая половина не менее важна первой: решение владельца звучит «пока
публичными», и авторизованный путь обязан ожить без переделки.

Зависимостей у файла нет: ``PyYAML`` в ``requirements.txt`` не входит,
поэтому блок ``run:`` вынимается разбором отступов, а не парсером YAML.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Собирается из кусков намеренно: цельная строка, похожая на токен, — это
# то, что detect-secrets обязан ловить, и приучать его к исключениям ради
# теста нельзя.
FAKE_VALUE = "gh" + "p_" + ("0" * 36)


@functools.lru_cache(maxsize=1)
def _auth_step_script() -> str:
    """Тело ``run:`` того шага, что настраивает git-авторизацию.

    Якорь — строка ``GH_DEPLOY_TOKEN: ${{ secrets.GH_DEPLOY_TOKEN }}``, а не
    имя шага: имя это свободный текст, и привязка к нему сделала бы страж
    хрупким к безобидному переименованию.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    anchors = [i for i, line in enumerate(lines) if "GH_DEPLOY_TOKEN: ${{" in line]
    assert len(anchors) == 1, (
        f"ожидался ровно один шаг, пробрасывающий GH_DEPLOY_TOKEN в env, "
        f"найдено {len(anchors)}. Если шаг раздвоился — раздвоится и поведение."
    )
    run_index = next(
        i for i in range(anchors[0], len(lines)) if lines[i].strip().startswith("run:")
    )
    body_indent = len(lines[run_index]) - len(lines[run_index].lstrip())
    strip_width = body_indent + 2
    body: list[str] = []
    for line in lines[run_index + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= body_indent:
            break
        body.append(line[strip_width:] if line.strip() else "")
    script = "\n".join(body)
    assert "insteadOf" in script, f"в теле шага нет переписывания URL:\n{script}"
    return script


@functools.lru_cache(maxsize=1)
def _usable_bash() -> str | None:
    """Путь к bash, который ПЕРЕДАЁТ окружение дочернему процессу.

    На раннере это ``/bin/bash``. На машине разработчика под Windows
    ``shutil.which("bash")`` нередко находит bash из WSL — тот живёт в своём
    пространстве окружения и не увидит ни ``GH_DEPLOY_TOKEN``, ни
    ``GIT_CONFIG_GLOBAL``, которые мы задаём. Тест на таком bash прошёл бы,
    ничего не проверив, и вдобавок мог бы записать переписывание URL в
    НАСТОЯЩИЙ ~/.gitconfig разработчика. Кандидат допускается, только если
    вернул пробный маркер.
    """
    candidates = [shutil.which("bash"), "/bin/bash"]
    git = shutil.which("git")
    if git:
        # Git for Windows кладёт свой bash рядом: <..>/Git/cmd/git.exe ->
        # <..>/Git/bin/bash.exe.
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        probe = subprocess.run(  # noqa: S603 — фиксированная команда, без ввода извне
            [candidate, "-c", 'printf %s "$AYLA_BASH_ENV_PROBE"'],
            capture_output=True,
            text=True,
            env={**os.environ, "AYLA_BASH_ENV_PROBE": "ok"},
            timeout=30,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


def _run_step(token: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Выполнить скрипт шага так, как его выполнит раннер.

    ``-e`` — не украшение: GitHub Actions запускает ``run:`` как
    ``bash --noprofile --norc -e -o pipefail``. Без ``-e`` упавший
    ``git config`` посреди скрипта остался бы незамеченным, потому что
    последней командой стоит ``echo``.

    ``GIT_CONFIG_GLOBAL`` уводится в tmp: ``git config --global`` внутри
    скрипта не должен трогать настоящий ~/.gitconfig разработчика.
    """
    bash = _usable_bash()
    assert bash is not None, "нет пригодного bash — тест должен был быть пропущен"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "GH_DEPLOY_TOKEN": token,
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(  # noqa: S603 — скрипт берётся из файла в этом же репозитории
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _auth_step_script()],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )


def test_step_is_conditional_and_never_fails_the_run() -> None:
    """Статически: ветвление по значению, без ``exit 1`` и без ``::error::``."""
    script = _auth_step_script()
    assert '-n "$GH_DEPLOY_TOKEN"' in script, (
        "переписывание URL должно происходить только при непустом значении — "
        "иначе в глобальный конфиг уходит https://@github.com/, собранный из "
        "значения, которое шаг ни разу не посмотрел (DRF-1466)."
    )
    assert "exit 1" not in script, (
        "шаг останавливает прогон при отсутствии токена. ayla-ai-core — "
        "публичный репозиторий, клонирование пройдёт анонимно; честный сигнал "
        "о недоступной зависимости даёт сам `pip install`, а не догадка перед "
        "ним (DRF-1466)."
    )
    assert "::error::" not in script, (
        "отсутствие необязательного токена помечено как ошибка. "
        "Предупреждение — да, ошибка — нет (DRF-1466)."
    )


@pytest.mark.skipif(_usable_bash() is None, reason="нет bash, передающего окружение")
def test_step_succeeds_without_a_token_and_writes_no_rewrite(tmp_path: Path) -> None:
    """Без токена: шаг проходит, переписывание URL не записано."""
    result = _run_step(token="", tmp_path=tmp_path)

    assert result.returncode == 0, (
        f"шаг упал без токена (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    written = (tmp_path / "gitconfig").read_text(encoding="utf-8")
    assert "insteadOf" not in written, (
        f"переписывание URL записано при пустом токене — в конфиг ушло: {written!r}"
    )


@pytest.mark.skipif(_usable_bash() is None, reason="нет bash, передающего окружение")
def test_step_still_configures_auth_when_a_token_is_present(tmp_path: Path) -> None:
    """С токеном: авторизованный путь цел, значение не утекло в вывод."""
    result = _run_step(token=FAKE_VALUE, tmp_path=tmp_path)

    assert result.returncode == 0, (
        f"шаг упал с токеном (rc={result.returncode}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    written = (tmp_path / "gitconfig").read_text(encoding="utf-8")
    assert "insteadOf" in written and FAKE_VALUE in written, (
        f"с непустым токеном переписывание URL не записано — в конфиг ушло: {written!r}"
    )
    assert FAKE_VALUE not in result.stdout + result.stderr, (
        "значение токена напечатано в журнал прогона. Маскирование раннером — "
        "не оправдание: шаг не должен его печатать."
    )
