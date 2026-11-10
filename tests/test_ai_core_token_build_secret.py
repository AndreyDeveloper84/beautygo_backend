"""Страж: токен ayla-ai-core — секрет BuildKit, а не build-arg (DRF-2996).

До правки токен ехал в сборку как ``ARG GH_DEPLOY_TOKEN`` и ложился в
``/root/.gitconfig`` через ``git config --global``. Три утечки: значение
build-arg печатает ``docker history`` и хранит ``docker buildx history``;
файл оставался в образе (одна стадия, без USER); через ``env_file: .env``
токен попадал в окружение каждого контейнера стенда.

Что держит страж:

* оба ``pip install`` идут под ``--mount=type=secret,id=gh_deploy_token`` и
  передают токен git только через ``GIT_CONFIG_COUNT/KEY/VALUE``;
* ``ARG``/``ENV`` с токеном в Dockerfile нет, ``git config`` в RUN нет;
* у трёх собираемых сервисов compose секрет объявлен и build-arg убран,
  переменная в окружении контейнера обнулена, секрет объявлен на верхнем
  уровне;
* шаг сборки в ci.yml передаёт файл секрета только команде ``build`` и
  отказывает, если токен вернулся в ``docker-compose.override.yml``;
* на собранной конфигурации compose с фальшивым токеном значение не видно
  ни в одном сервисе (``docker compose config``; пропуск только без docker).

Каждая проверка отсутствия идёт после проверки наличия на тех же данных.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "docker-compose.yml"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

TOKEN_VAR = "GH_DEPLOY_TOKEN"
SECRET_ID = "gh_deploy_token"
SECRET_MOUNT = f"--mount=type=secret,id={SECRET_ID}"
BUILT_SERVICES = ("web", "celery_worker", "celery_beat")
#: Все сервисы с `env_file: .env` — туда же уходит и строка токена, пока она в файле.
ENV_FILE_SERVICES = BUILT_SERVICES + ("db",)
FAKE_VALUE = "ghp_fake_token_for_the_guard_0123456789abcdef"


def _dockerfile_instructions() -> list[str]:
    """Инструкции Dockerfile: строки-продолжения склеены, комментарии убраны."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    joined = re.sub(r"\\\n", " ", text)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _pip_install_runs() -> list[str]:
    instructions = _dockerfile_instructions()
    assert any(i.startswith("FROM ") for i in instructions), "Dockerfile не разобран"
    runs = [i for i in instructions if i.startswith("RUN ") and "pip install" in i and "requirements" in i]
    assert runs, "в Dockerfile нет RUN с `pip install -r requirements`"
    return runs


def _compose() -> dict:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert set(BUILT_SERVICES) <= set(data["services"]), "в compose нет собираемых сервисов"
    return data


def _build_step_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]["steps"]
    scripts = [s["with"]["script"] for s in deploy if "docker compose" in s.get("with", {}).get("script", "")]
    build = [s for s in scripts if " build web celery_worker celery_beat" in s]
    assert len(build) == 1, "ожидался ровно один шаг deploy, собирающий web celery_worker celery_beat"
    return build[0]


# ─── Dockerfile ─────────────────────────────────────────────────────────────


def test_pip_install_mounts_the_build_secret_and_hands_it_to_git_by_environment() -> None:
    for run in _pip_install_runs():
        assert SECRET_MOUNT in run, "pip install без секрета BuildKit: клон приватного ayla-ai-core упадёт"
        assert f"-s /run/secrets/{SECRET_ID}" in run, "токен должен использоваться только при непустом секрете"
        assert "GIT_CONFIG_COUNT=1" in run and "GIT_CONFIG_KEY_0=" in run and "insteadOf" in run, (
            "секрет смонтирован, но git его не получает: нет GIT_CONFIG_COUNT/KEY/VALUE"
        )


def test_dockerfile_carries_the_token_neither_as_arg_nor_as_env() -> None:
    instructions = _dockerfile_instructions()
    assert any(i.startswith("FROM ") for i in instructions), "Dockerfile не разобран"
    carriers = [i for i in instructions if re.match(rf"(ARG|ENV)\s+{TOKEN_VAR}\b", i)]
    assert carriers == [], f"токен снова build-arg или ENV: {carriers!r} — значение видно в docker history"


@pytest.mark.parametrize("forbidden", ["git config", ".git-credentials", "credential.helper", "set -x", "printenv"])
def test_secret_runs_neither_persist_nor_print_the_token(forbidden: str) -> None:
    for run in _pip_install_runs():
        # empty-assert-ok: _pip_install_runs сам падает, если RUN с pip install нет
        assert forbidden not in run, f"`{forbidden}` в RUN с секретом: токен ляжет на диск или в лог сборки"


def test_dockerfile_has_no_syntax_directive() -> None:
    """`# syntax=` тянет фронтенд с Docker Hub, который пилот достаёт плохо."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.lstrip().startswith(("#", "ARG", "FROM")), "Dockerfile не прочитан"
    assert not re.search(r"^#\s*syntax\s*=", text, re.MULTILINE | re.IGNORECASE)


# ─── compose ────────────────────────────────────────────────────────────────


def test_every_built_service_declares_the_secret_and_no_token_build_arg() -> None:
    services = _compose()["services"]
    for name in BUILT_SERVICES:
        build = services[name]["build"]
        assert build.get("secrets") == [SECRET_ID], f"{name}: build.secrets без {SECRET_ID}"
        args = build.get("args") or {}
        assert "PYTHON_BASE_IMAGE" in args, f"{name}: build.args не разобраны"
        assert TOKEN_VAR not in args, f"{name}: {TOKEN_VAR} снова build-arg"


def test_every_service_reading_dot_env_blanks_the_token_in_its_environment() -> None:
    """`.env` подключён как env_file; пока в нём строка токена, её перекрывает `environment`."""
    services = _compose()["services"]
    readers = [n for n, s in services.items() if ".env" in (s.get("env_file") or [])]
    assert sorted(readers) == sorted(ENV_FILE_SERVICES), f"env_file .env читают {readers!r}: обновить ENV_FILE_SERVICES"
    for name in readers:
        env = services[name]["environment"]
        assert env, f"{name}: environment пуст"
        blanked = f"{TOKEN_VAR}=" in env if isinstance(env, list) else env.get(TOKEN_VAR) == ""
        assert blanked, f"{name}: в environment нет пустого {TOKEN_VAR}"


def test_the_secret_is_declared_at_top_level_from_the_environment() -> None:
    secrets = _compose().get("secrets") or {}
    assert secrets.get(SECRET_ID) == {"environment": TOKEN_VAR}, "секрет сборки не объявлен или читается не из env"


# ─── ci.yml ─────────────────────────────────────────────────────────────────


def test_deploy_hands_the_build_env_file_to_build_only_and_refuses_an_override_with_the_token() -> None:
    script = _build_step_script()
    assert "BUILD_ENV=" in script and ".config/beautygo/build.env" in script, "шаг не знает файла секрета сборки"
    assert 'docker compose "${BUILD_ENV_ARGS[@]}" build web celery_worker celery_beat' in script, (
        "файл секрета должен попадать в compose только у команды build"
    )
    assert script.count("BUILD_ENV_ARGS[@]") == 1, "файл секрета нужен только `build`"
    assert "--env-file .env --env-file" in script, "`--env-file` заменяет неявный .env — нужно назвать оба файла"
    assert "grep -q 'GH_DEPLOY_TOKEN' docker-compose.override.yml" in script and "::error::" in script, (
        "шаг не отказывает, если токен вернулся build-arg в override на хосте"
    )
    assert "--build-arg" not in script, "`--build-arg` в шаге сборки: значение уйдёт в историю BuildKit"


def test_no_workflow_passes_the_token_as_a_build_arg() -> None:
    files = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    assert files, "workflow-файлы не найдены"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"{path.name} пуст"
        assert not re.search(rf"--build-arg\s+{TOKEN_VAR}", text), f"{path.name}: `--build-arg {TOKEN_VAR}`"


# ─── собранная конфигурация ─────────────────────────────────────────────────


def test_resolved_config_keeps_the_fake_token_out_of_every_service(tmp_path: Path) -> None:
    if shutil.which("docker") is None:  # pragma: no cover - depends on the host
        pytest.skip("нет docker")
    shutil.copy(COMPOSE, tmp_path / COMPOSE.name)
    (tmp_path / ".env").write_text(
        f"{TOKEN_VAR}={FAKE_VALUE}\nPOSTGRES_PASSWORD=x\nMINIO_ROOT_USER=x\nMINIO_ROOT_PASSWORD=x\n"
        "DJANGO_ALLOWED_HOSTS=localhost\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 — фиксированная команда, без ввода извне
        ["docker", "compose", "-f", COMPOSE.name, "config"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    resolved = yaml.safe_load(result.stdout)
    assert set(BUILT_SERVICES) <= set(resolved["services"]), "compose config не отдал собираемые сервисы"
    for name in BUILT_SERVICES:
        build = resolved["services"][name]["build"]
        assert SECRET_ID in [s.get("source") for s in build.get("secrets") or []], f"{name}: секрет потерян"
    for name, service in resolved["services"].items():
        assert service.get("environment", {}).get(TOKEN_VAR, "") == "", f"{name}: токен виден процессу контейнера"
    assert resolved["secrets"][SECRET_ID].get("environment") == TOKEN_VAR
    assert FAKE_VALUE not in result.stdout, "фальшивый токен виден в собранной конфигурации"
